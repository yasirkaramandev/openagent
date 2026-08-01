# Add a priority filter

Work only inside this generated Task Board workspace and obey `OPENAGENT.md`.

Add an optional `--priority {low,medium,high}` filter to the `list` CLI command.
Keep the service API useful without coupling it to argparse. Add offline tests
for each priority and for the unfiltered behavior. Preserve the JSON output
shape and the atomic persistence format.

Run the complete test suite before finishing. Do not access the network, read
secrets, install packages, or modify files outside this workspace.

