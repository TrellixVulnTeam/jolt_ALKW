from contextlib import contextmanager
import copy
import smtplib
from email.message import EmailMessage
import os

from jolt import config
from jolt import filesystem as fs
from jolt import log
from jolt import utils
from jolt.error import raise_error_if
from jolt.plugins import report
from jolt.hooks import CliHook, CliHookFactory


class EmailHooks(CliHook):
    def __init__(self):
        self._server = config.get("email", "server")
        self._to = config.get("email", "to")
        self._from = config.get("email", "from", "jolt@localhost")
        self._subject = config.get("email", "subject", "Jolt Build Report")
        self._stylesheet = config.get(
            "email", "stylesheet",
            fs.path.join(fs.path.dirname(__file__), "email.xslt"))
        self._artifact = config.get("email", "artifact")
        self._failure = config.getboolean("email", "on_failure", True)
        self._success = config.getboolean("email", "on_success", True)
        raise_error_if(not self._server, "email.server not configured")
        raise_error_if(not self._server, "email.to not configured")

    def annotate_report(self, report):
        # Make environment variables available to stylesheet
        for key, value in os.environ.items():
            param = report.create_parameter()
            param.key = key
            param.value = value

    def send_report(self, report):
        self.annotate_report(report)

        if self._artifact:
            with open(self._artifact, "w") as f:
                f.write(report.transform(self._stylesheet))

        if report.has_failure():
            if not self._failure:
                return
        else:
            if not self._success:
                return

        msg = EmailMessage()
        msg['Subject'] = self._subject
        msg['From'] = self._from
        msg['To'] = ", ".join(self._to.split())
        msg.set_content("Your e-mail client cannot display HTML formatted e-mails.")
        msg.add_alternative(report.transform(self._stylesheet), subtype='html')

        with smtplib.SMTP(self._server) as server:
            log.info("Sending email report to {}", self._to)
            server.send_message(msg)

    @contextmanager
    def cli_build(self, *args, **kwargs):
        start = utils.duration()
        try:
            yield
        finally:
            with report.update() as manifest:
                manifest = copy.copy(manifest)
                manifest.duration = str(int(start.seconds))
                self.send_report(manifest)


@CliHookFactory.register
class ResultFactory(CliHookFactory):
    def create(self, env):
        return EmailHooks()
