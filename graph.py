from tasks import *
from utils import *
from influence import *
from copy import copy
import hashlib
from tools import Tools
import networkx as nx
from networkx.drawing.nx_agraph import write_dot
import log
import utils
import traceback


class TaskProxy(object):
    def __init__(self, task, graph):
        self.task = task
        self.graph = graph
        self.children = []
        self.ancestors = []
        self.extensions = []

        self._extended_task = None
        self._in_progress = False
        self._completed = False

    def __hash__(self):
        return id(self)

    @property
    def tools(self):
        return self.task.tools

    @property
    def name(self):
        return self.task.name

    @property
    def canonical_name(self):
        return self.task.name.replace("/", "_")

    @property
    def qualified_name(self):
        return utils.format_task_name(self.task.name, self.task._get_parameters())

    @property
    def log_name(self):
        return "({} {})".format(self.qualified_name, self.identity[:8])

    @property
    @cached.instance
    def identity(self):
        sha = hashlib.sha1()

        HashInfluenceRegistry.get().apply_all(self.task, sha)

        # print("{}: {}".format(self.name, [n.name for n in self.children]))
        for node in self.children:
            sha.update(node.identity)

        if self._extended_task:
            sha.update(self._extended_task.identity)

        return sha.hexdigest()

    def __str__(self):
        return "{}{}".format(self.qualified_name, "*" if self.is_extension() else '')

    def info(self, fmt, *args, **kwargs):
        self.task.info(fmt + " " + self.log_name, *args, **kwargs)

    def warn(self, fmt, *args, **kwargs):
        self.task.warn(fmt + " " + self.log_name, *args, **kwargs)

    def error(self, fmt, *args, **kwargs):
        self.task.error(fmt + " " + self.log_name, *args, **kwargs)

    def has_children(self):
        return len(self.children) > 0

    def has_ancestors(self):
        return len(self.ancestors) > 0

    def is_cacheable(self):
        return self.task.is_cacheable()

    def is_resource(self):
        return isinstance(self.task, Resource)

    def has_extensions(self):
        return len(self.extensions) > 0

    def add_extension(self, task):
        if self.is_extension():
            self._extended_task.add_extension(task)
        else:
            self.extensions.append(task)

    def is_extension(self):
        return self._extended_task is not None

    def set_extended_task(self, task):
        self._extended_task = task

    def get_extended_task(self):
        if self.is_extension():
            return self._extended_task.get_extended_task()
        return self

    def in_progress(self):
        return self._in_progress

    def is_ready(self):
        if self.in_progress():
            return False

        if self.is_extension():
            return False

        return self.graph.is_leaf(self)

    def is_completed(self):
        return self._completed

    def is_cached(self, cache, network):
        for extension in self.extensions:
            if not cache.is_available(extension, network):
                return False
        return cache.is_available(self, network)
    
    def set_in_progress(self):
        self._in_progress = True

    def finalize(self, dag):
        # Find all direct and transitive dependencies
        self.children = sorted(nx.descendants(dag, self), key=lambda t: t.qualified_name)

        # Exclude transitive resources dependencies
        self.children = filter(
            lambda n: not n.is_resource() or dag.are_neighbors(self, n),
            self.children)

        self.anestors = nx.ancestors(dag, self)
        return self.identity

    def started(self):
        self.info("Execution started")
        self.duration = utils.duration()

    def failed(self):
        self.error("Execution failed after {}", self.duration)
        log.verbose(traceback.format_exc())

    def finished(self):
        assert not self._completed, "task has already been completed"
        self._completed = True
        try:
            self.graph.remove_node(self)
        except:
            self.warn("Pruned task was executed")
        self.info("Execution finished after {}", self.duration)

    def run(self, cache, force_upload=False, force_build=False):
        with self.tools:
            if not force_build and cache.is_available_remotely(self):
                cache.download(self)

            if force_build or not cache.is_available_locally(self) or self.has_extensions():
                with cache.get_context(self) as context:
                    with self.tools.cwd(self.task.joltdir):
                        self.task.run(context, self.tools)

                if cache.is_available_locally(self):
                    with cache.get_artifact(self) as artifact:
                        artifact.discard()

                with cache.get_artifact(self) as artifact:
                    with self.tools.cwd(self.task.joltdir):
                        self.task.publish(artifact, self.tools)
                    artifact.commit()

                assert cache.upload(self, force=force_upload), \
                    "Failed to upload artifact for {}".format(self.name)

                for extension in self.extensions:
                    try:
                        extension.started()
                        extension.run(cache, force_upload, force_build=True)
                    except:
                        extension.failed()
                        raise
                    else:
                        extension.finished()


class Graph(nx.DiGraph):
    def __init__(self):
        super(Graph, self).__init__()

    def prune(self, func):
        for node in [n for n in self.nodes]:
            log.hysterical("[GRAPH] Checking {} ({})", node.name, node.identity)
            if func(self, node):
                log.hysterical("[GRAPH] Pruned {}", node.name)
                self.remove_node(node)

    def select(self, func):
        return [n for n in self.nodes if func(self, n)]

    def is_leaf(self, node):
        return self.out_degree(node) == 0
    
    def is_root(self, node):
        return self.in_degree(node) == 0

    def are_neighbors(self, n1, n2):
        return n2 in self[n1]


class GraphBuilder(object):
    def __init__(self):
        self.graph = Graph()
        self.nodes = {}
        self.registry = tasks.TaskRegistry.get()

    def _get_node(self, name):
        node = self.nodes.get(name)
        if not node:
            task = self.registry.get_task(name)
            node = self._build_node(TaskProxy(task, self.graph))
            self.nodes[name] = node
        return node
        
    def _build_node(self, node):
        self.graph.add_node(node)

        extended = node.task._get_extends()
        if extended:
            extended_node = self._get_node(extended)
            self.graph.add_edges_from([(node, extended_node)])
            node.set_extended_task(extended_node)
            extended_node.add_extension(node)
            parent = extended_node.get_extended_task()
        else:
            parent = node

        for requirement in node.task._get_requires():
            child = self._get_node(requirement)
            self.graph.add_edges_from([(parent, child)])

        node.finalize(self.graph)
        return node

    def build(self, task_list):
        proxies = [self._get_node(task) for task in task_list]
        assert nx.is_directed_acyclic_graph(self.graph), "cyclic graph"
        return self.graph

    def display(self):
        t = tools.Tools()
        with t.tmpdir("dot") as tmpdir, t.cwd(tmpdir.get_path()):
            write_dot(self.graph, fs.path.join(t.getcwd(), 'graph.dot'))
            t.run('dot -Tsvg graph.dot -o graph.svg')
            t.run('eog graph.svg')
