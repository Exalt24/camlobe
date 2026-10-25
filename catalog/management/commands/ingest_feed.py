import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.models import PipelineRun, Supplier
from catalog.runs import AlreadyRunning


class Command(BaseCommand):
    help = (
        "Ingest one feed for one supplier from a local file, an SFTP server, a paginated REST API or a SOAP service. "
        "Restartable: it resumes at its checkpoint, and running it again over the same source changes nothing. "
        "Secrets are read from environment variables, never from the command line."
    )

    def add_arguments(self, parser):
        parser.add_argument("supplier", help="supplier code, created on first use")
        parser.add_argument("--name", default="", help="display name for a new supplier")
        parser.add_argument("--batch-size", type=int, default=1000)
        src = parser.add_argument_group("source (one of)")
        src.add_argument("--file", help="a local CSV or XML file")
        src.add_argument("--format", choices=["csv", "xml"], default=None, help="for --file and --sftp-path")
        src.add_argument("--sftp-host")
        src.add_argument("--sftp-port", type=int, default=22)
        src.add_argument("--sftp-path", help="remote path of a CSV or XML file")
        src.add_argument("--sftp-user")
        src.add_argument("--sftp-password-env", help="name of the environment variable holding the password")
        src.add_argument("--sftp-key", help="path to a private key file")
        src.add_argument("--sftp-host-key", help="expected host key fingerprint, SHA256:... (required)")
        src.add_argument("--rest-url", help="a paginated JSON API: GET url?offset=&limit= -> {items, total}")
        src.add_argument("--token-env", help="name of the environment variable holding a bearer token")
        src.add_argument("--soap-wsdl", help="WSDL URL of a SOAP service with a GetCatalogPage operation")
        parser.add_argument("--page-size", type=int, default=100, help="for --rest-url and --soap-wsdl")
        parser.add_argument("--rate-limit", type=float, default=None, help="requests per second for --rest-url and --soap-wsdl")

    def build_adapter(self, opts):
        chosen = [k for k in ("file", "sftp_host", "rest_url", "soap_wsdl") if opts[k]]
        if len(chosen) != 1:
            raise CommandError("give exactly one source: --file, --sftp-host, --rest-url or --soap-wsdl")
        if opts["file"]:
            path = Path(opts["file"])
            if not path.is_file():
                raise CommandError(f"no such file: {path}")
            return FileAdapter(path, fmt=opts["format"])
        if opts["sftp_host"]:
            from catalog.ingest.sftp import SftpAdapter

            if not (opts["sftp_path"] and opts["sftp_user"] and opts["sftp_host_key"]):
                raise CommandError("--sftp-host needs --sftp-path, --sftp-user and --sftp-host-key")
            password = os.environ.get(opts["sftp_password_env"] or "") if opts["sftp_password_env"] else None
            if opts["sftp_password_env"] and not password:
                raise CommandError(f"environment variable {opts['sftp_password_env']} is empty or not set")
            try:
                return SftpAdapter(
                    opts["sftp_host"], opts["sftp_path"], opts["sftp_user"], opts["sftp_host_key"],
                    password=password, key_filename=opts["sftp_key"], port=opts["sftp_port"], fmt=opts["format"],
                )
            except ValueError as exc:
                raise CommandError(str(exc))
        token = os.environ.get(opts["token_env"]) if opts["token_env"] else None
        if opts["token_env"] and not token:
            raise CommandError(f"environment variable {opts['token_env']} is empty or not set")
        common = {"page_size": opts["page_size"], "rate_limit_per_sec": opts["rate_limit"]}
        try:
            if opts["rest_url"]:
                from catalog.ingest.rest import RestAdapter

                return RestAdapter(opts["rest_url"], token=token, **common)
            from catalog.ingest.soap import SoapAdapter

            return SoapAdapter(opts["soap_wsdl"], **common)
        except ValueError as exc:
            raise CommandError(str(exc))

    def handle(self, *args, **opts):
        supplier, _ = Supplier.objects.get_or_create(code=opts["supplier"], defaults={"name": opts["name"] or opts["supplier"]})
        adapter = self.build_adapter(opts)
        try:
            run = ingest(supplier, adapter, batch_size=opts["batch_size"])
        except AlreadyRunning:
            raise CommandError("another ingest of this source is running")
        finally:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup:
                cleanup()
        self.stdout.write(
            f"ingest run {run.pk} {run.status}: read={run.rows_read} stored={run.rows_written} "
            f"unchanged={run.rows_unchanged} rejected={run.rows_rejected}"
            + (f" resumed_from={run.notes['resumed_from']}" if "resumed_from" in run.notes else "")
            + (f" ({run.notes['skipped']})" if "skipped" in run.notes else "")
        )
        if run.status != PipelineRun.Status.SUCCEEDED:
            for err in run.errors[:3]:
                self.stderr.write(f"  {err['where']}: {err['message']}")
            raise CommandError(f"run {run.pk} ended {run.status}")
