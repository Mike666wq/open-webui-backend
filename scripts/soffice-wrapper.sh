#!/bin/sh
# LibreOffice serializes on $HOME/.config/libreoffice. Give each conversion its
# own temporary HOME so concurrent PPT/DOC uploads cannot fight over a profile.
set -eu
profile_dir=$(mktemp -d /tmp/lo-profile.XXXXXX)
cleanup() { rm -rf "$profile_dir"; }
trap cleanup EXIT INT TERM

HOME="$profile_dir" /usr/lib/libreoffice/program/soffice "$@"
