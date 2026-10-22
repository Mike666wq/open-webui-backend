"""Exercise the same isolated Compose environment used for manual cloud testing."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import urllib.request


def main():
    image = os.environ['WEBUI_TEST_IMAGE']
    if not re.fullmatch(r'ghcr.io/mike666wq/open-webui-backend@sha256:[0-9a-f]{64}', image):
        raise ValueError('Compose CI tests require the immutable backend image digest')
    project = 'owui-ci-' + secrets.token_hex(6)
    command = ['docker', 'compose', '-p', project, '-f', 'compose.test.yml']
    output = Path('artifacts/compose-test')
    output.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        subprocess.run([*command, 'up', '-d', '--wait', '--wait-timeout', '300'], check=True)
        with opener.open('http://127.0.0.1:8080/health', timeout=15) as response:
            assert json.load(response)['status'] is True
        # Disposable first-user signup validates the manual testing entry point.
        password = secrets.token_urlsafe(32)
        payload = json.dumps(dict(name='CI Tester', email='compose-test@example.com',
                                  password=password, password_confirmation=password)).encode()
        request = urllib.request.Request('http://127.0.0.1:8080/api/v1/auths/signup',
                                         data=payload, headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
            assert result.get('token'), 'Test signup did not return a login token'
        (output/'report.json').write_text(json.dumps(dict(image=image, health=True, signup=True), indent=2)+'\n')
    finally:
        # Backend logs can contain generated test credentials; retain only the report.
        subprocess.run([*command, 'down', '-v'], check=True)


if __name__ == '__main__':
    main()
