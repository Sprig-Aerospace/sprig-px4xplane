#!/bin/sh
# Review the checkout and approve the exact Pipeline trust prompt before setup.
set -eu
exec pipeline onboarding setup "$PWD" --consent --gate manual "$@"
