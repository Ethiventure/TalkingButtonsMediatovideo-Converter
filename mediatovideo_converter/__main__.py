"""Route module launches through the same validated application entry point."""

from run_app import run_app_main


if __name__ == "__main__":
    raise SystemExit(run_app_main())
