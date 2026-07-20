"""Process entrypoint (see Dockerfile CMD) -- NOT app/main.py.

The stdout/stderr tee (app/logsetup.py) must be installed before uvicorn's
own Config/logging setup runs, or uvicorn's handlers end up bound to the
original stream objects (captured at handler-creation time) and everything
it logs itself -- including the access log and exception tracebacks --
silently bypasses the tee. Invoking uvicorn programmatically here, after
installing the tee, guarantees the ordering; `uvicorn app.main:app` from the
CLI does not.
"""

import uvicorn

from app.config import settings
from app.logsetup import install_log_tee

install_log_tee(settings.log_dir)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
