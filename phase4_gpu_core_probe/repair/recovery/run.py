"""Compatibility entry for the installed HERM0S supervisor."""

from hermos_runtime.supervision.run import main

if __name__ == "__main__":
    raise SystemExit(main())
