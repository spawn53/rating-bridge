"""Legacy entry point for the safe interactive Simkl authorization helper."""

from bootstrap_provider_auth import main

if __name__ == "__main__":
    raise SystemExit(main(["simkl"]))
