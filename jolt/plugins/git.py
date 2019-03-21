from jolt.tasks import *
from jolt.influence import *
from jolt.tools import *
from jolt.scheduler import *
from jolt.loader import JoltLoader
from jolt import utils
from jolt import filesystem as fs

try:
    import pygit2
    has_pygit = True
except:
    has_pygit = False



class GitRepository(object):
    def __init__(self, url, path, relpath, refspecs=None):
        self.path = path
        self.relpath = relpath
        self.tools = Tools()
        self.url = url
        self.refspecs = utils.as_list(refspecs)

    @utils.cached.instance
    def _get_git_folder(self):
        return fs.path.join(self.path, ".git")

    def is_cloned(self):
        return fs.path.exists(self._get_git_folder())

    def _is_synced(self):
        with self.tools.cwd(self.path):
            return True if self.tools.run("git branch -r --contains HEAD", output_on_error=True) else False
        return True

    def clone(self):
        assert not fs.path.exists(self.path), \
            "destination folder '{0}' already exists but is not a git repo"\
            .format(self.path)
        log.info("Cloning into {0}", self.path)
        self.tools.run("git clone {0} {1}", self.url, self.path, output_on_error=True)
        assert fs.path.exists(self._get_git_folder()),\
            "failed to clone git repo '{0}'".format(self.relpath)

    @utils.cached.instance
    def _diff(self, path="/"):
        with self.tools.cwd(self.path):
            return self.tools.run("git diff HEAD .{0}".format(path), output_on_error=True)

    def diff(self, path="/"):
        return self._diff(path) if self.is_cloned() else ""

    @utils.cached.instance
    def _head(self):
        with self.tools.cwd(self.path):
            return self.tools.run("git rev-parse HEAD", output_on_error=True)

    def head(self):
        return self._head() if self.is_cloned() else ""

    @utils.cached.instance
    def diff_hash(self, path="/"):
        return utils.sha1(self.diff(path))

    @utils.cached.instance
    def tree_hash(self, sha="HEAD", path="/"):
        with self.tools.cwd(self.path):
            try:
                return self.tools.run("git rev-parse {0}:.{1}".format(sha, path), output=False)
            except:
                self.fetch()
                return self.tools.run("git rev-parse {0}:.{1}".format(sha, path), output_on_error=True)

    def clean(self):
        with self.tools.cwd(self.path):
            return self.tools.run("git clean -fd", output_on_error=True)

    def reset(self):
        with self.tools.cwd(self.path):
            return self.tools.run("git reset --hard", output_on_error=True)

    def fetch(self):
        refspecs = self.refspecs or []
        for refspec in [''] + refspecs:
            self.tools.run("git fetch {url} {refspec}",
                           url=self.url,
                           refspec=refspec or '')

    def checkout(self, rev):
        log.info("Checkout out {0} in {1}", rev, self.path)
        with self.tools.cwd(self.path):
            try:
                return self.tools.run("git checkout -f {rev}", rev=rev, output=False)
            except:
                self.fetch()
                return self.tools.run("git checkout -f {rev}", rev=rev, output_on_error=True)


_repositories = {}

def _create_repo(url, path, relpath, refspecs=None):
    repo = _repositories.get(relpath)
    if not repo:
        repo = GitRepository(url, path, relpath, refspecs)
        _repositories[relpath] = repo
    return repo



class GitInfluenceProvider(HashInfluenceProvider):
    name = "Tree"

    def __init__(self, path):
        super(GitInfluenceProvider, self).__init__()
        self.tools = Tools()
        self.relpath = path

    @property
    def path(self):
        return fs.path.join(JoltLoader.get().joltdir, self.relpath)

    def diff(self):
        with self.tools.cwd(self.path):
            return self.tools.run("git diff HEAD .", output_on_error=True)

    def diff_hash(self):
        return utils.sha1(self.diff())

    def tree_hash(self, sha="HEAD", path="/"):
        with self.tools.cwd(self.path):
            return self.tools.run("git rev-parse {0}:.{1}".format(sha, path), output_on_error=True)

    @utils.cached.instance
    def get_influence(self, task):
        return "{0}:{1}:{2}".format(
            self.relpath,
            self.tree_hash(),
            self.diff_hash()[:8])


def global_influence(path, cls=GitInfluenceProvider):
    HashInfluenceRegistry.get().register(cls(path))


def influence(path, cls=GitInfluenceProvider):
    def _decorate(taskcls):
        if "influence" not in taskcls.__dict__:
            taskcls.influence = copy.copy(taskcls.influence)
        provider = cls(path=path)
        taskcls.influence.append(provider)
        return taskcls
    return _decorate


class GitSrc(Resource):
    """ Clones a Git repo.
    """

    name = "git-src"
    url = Parameter(help="URL to the git repo to be cloned. Required.")
    sha = Parameter(required=False, help="Specific commit or tag to be checked out. Optional.")
    path = Parameter(required=False, help="Local path where the repository should be cloned.")
    _revision = Export(value=lambda self: self._get_revision() or self.git.head())

    def __init__(self, *args, **kwargs):
        super(GitSrc, self).__init__(*args, **kwargs)
        self.joltdir = JoltLoader.get().joltdir
        self.relpath = str(self.path) or self._get_name()
        self.abspath = fs.path.join(self.joltdir, self.relpath)
        self.refspecs = kwargs.get("refspecs", [])
        self.git = GitRepository(self.url, self.abspath, self.relpath, self.refspecs)

    @utils.cached.instance
    def _get_name(self):
        repo = fs.path.basename(self.url.get_value())
        name, _ = fs.path.splitext(repo)
        return name

    def _get_revision(self):
        if self._revision.value is not None:
            return self._revision.value
        if not self.sha.is_unset():
            return self.sha.get_value()
        return None

    def acquire(self, artifact, env, tools):
        if not self.git.is_cloned():
            self.git.clone()
        rev = self._get_revision()
        if rev is not None:
            assert self.sha.is_unset() or not self.git.diff(), \
                "explicit sha requested but git repo '{0}' has local changes"\
                .format(self.git.relpath)
            # Should be safe to do this now
            self.git.checkout(rev)
            self.git.clean()

TaskRegistry.get().add_task_class(GitSrc)


class Git(GitSrc, HashInfluenceProvider):
    """ Clones a Git repo.

    Also influences the hash of consuming tasks, causing tasks to
    be re-executed if the cloned repo is modified.

    """
    name = "git"
    url = Parameter(help="URL to the git repo to be cloned. Required.")
    sha = Parameter(required=False, help="Specific commit or tag to be checked out. Optional.")
    path = Parameter(required=False, help="Local path where the repository should be cloned.")
    _revision = Export(value=lambda self: self._get_revision())

    def __init__(self, *args, **kwargs):
        super(Git, self).__init__(*args, **kwargs)
        self.influence.append(self)

    @utils.cached.instance
    def get_influence(self, task):
        if not self.git.is_cloned():
            self.git.clone()
        rev = self._get_revision()
        rev = self._get_revision()
        if rev is not None:
            return self.git.tree_hash(rev)
        return "{0}:{1}:{2}".format(
            fs.path.join(self.git.relpath, str(self.path)),
            self.git.tree_hash(),
            self.git.diff_hash()[:8])

TaskRegistry.get().add_task_class(Git)


class GitNetworkExecutorExtension(NetworkExecutorExtension):
    """ Sanity check that a local git repo can be built remotely """

    def get_parameters(self, task):
        return {}
        for child in task.children:
            if isinstance(child.task, GitSrc):
                task = child.task
                if task.git.is_cloned() and task.sha.is_unset():
                    assert task._is_synced(),\
                        "local commit found in git repo '{0}'; "\
                        "push before building remotely"\
                        .format(task._get_name())
                    assert not task._get_diff(task),\
                        "local changes found in git repo '{0}'; "\
                        "commit and push before building remotely"\
                        .format(task._get_name())
        return {}


@NetworkExecutorExtensionFactory.Register
class GitNetworkExecutorExtensionFactory(NetworkExecutorExtensionFactory):
    def create(self):
        return GitNetworkExecutorExtension()
