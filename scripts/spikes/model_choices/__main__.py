"""Entry point for `python -m scripts.spikes.model_choices`."""

import sys

from scripts.spikes.model_choices.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
