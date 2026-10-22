"""Manual final-stage test: no shell interpolation, no mutable reference during smoke."""
import json
import os
import re
import subprocess
import sys

selection = os.environ['IMAGE_SELECTION']
if not re.fullmatch(r'latest|sha-[0-9a-f]{40}|sha256:[0-9a-f]{64}', selection):
    raise SystemExit('Expected latest, sha-<40 lowercase hex>, or sha256:<64 lowercase hex>')
image = 'ghcr.io/mike666wq/open-webui-backend' + ('@' if selection.startswith('sha256:') else ':') + selection
subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image], check=True)
metadata = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
digest = next(value for value in metadata['RepoDigests'] if value.startswith('ghcr.io/mike666wq/open-webui-backend@sha256:'))
subprocess.run([sys.executable, 'scripts/image_smoke.py', '--image', digest], check=True)
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write('Tested immutable image: `' + digest + '`\n')
