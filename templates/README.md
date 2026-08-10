# Legacy Templates

`bootstrap_supervisor.md` is retained only for the legacy mock loop in
`src/dispatcher/loop.py`. The validated sequential coordinator packages and
uses `src/dispatcher/templates/bootstrap_supervisor.md` instead.

`resume_context.md` is historical and has no current runtime consumer. Do not
add new behavior to either file. Current protocol and operations guidance lives
in `docs/` and the packaged template.
