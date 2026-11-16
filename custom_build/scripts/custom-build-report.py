# Copied once into the Hermes home's scripts/ folder, where Hermes cron runs it on Hermes's own Python
# (OPERATIONS.md section 4). It never changes: the report's code ships with the plugin.
import sys

from hermes_cli.main import main

sys.argv = ["hermes", "custom-build", "report"]
main()
