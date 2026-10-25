from django.core.management.base import BaseCommand, CommandError

from catalog.edi.x12 import X12Error, parse


class Command(BaseCommand):
    help = "Read an X12 file (850, 855, 856 or 810) and print what it says, or the first error and exit 1."

    def add_arguments(self, parser):
        parser.add_argument("file")

    def handle(self, *args, **opts):
        try:
            with open(opts["file"], encoding="ascii", newline="") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise CommandError(f"cannot read {opts['file']}: {exc}")
        try:
            env = parse(text)
        except X12Error as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"interchange {env.control_number} from {env.sender!r} to {env.receiver!r} ({len(env.documents)} documents)")
        for doc in env.documents:
            self.stdout.write(f"  {type(doc).__name__}: {doc}")
