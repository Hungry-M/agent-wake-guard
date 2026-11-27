"""Entry point for the separately distributed Huawei Hiview device plugin."""

import os
import sys

from device_plugins.huawei import ACTIVE_STATUS, session
from device_plugins.worker import serve
from i18n import LANGUAGE_ENV, select_language, tr


if __name__ == "__main__":
    try:
        select_language(os.environ.get(LANGUAGE_ENV, "auto"))
    except KeyError:
        select_language()
    if sys.argv[1:] == ["--guard-plugin-v1"]:
        raise SystemExit(serve(session, tr(ACTIVE_STATUS)))
    print(tr("Put this executable in the Guard's .plugins directory."), file=sys.stderr)
    raise SystemExit(2)
