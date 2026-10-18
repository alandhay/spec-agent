#!/bin/bash
# Check the hosted site from outside, signed in as a throwaway test person:
# the agent builds a program, the instance is put to sleep and woken again.
#     bash deploy/check.sh            (Git Bash; needs the AWS CLI signed in)
set -uo pipefail
out() { aws cloudformation describe-stacks --stack-name specdev --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text --no-cli-pager; }
URL=$(out Url); URL=${URL%/}; POOL=$(out UserPoolId); CLIENT=$(out UserPoolClientId); INSTANCE=$(out InstanceId)
WHO=specdev-check@example.com
PY="$(dirname "$0")/../.venv/Scripts/python.exe"
SECRET="$("$PY" -c "import secrets; print('Aa1!' + secrets.token_urlsafe(18))")"
state() { aws ec2 describe-instances --instance-ids "$INSTANCE" --query "Reservations[0].Instances[0].State.Name" --output text --no-cli-pager; }
remote() { "$PY" "$(dirname "$0")/deploy.py" run "$1" | grep -v '^\[Success\]'; }

aws cognito-idp admin-delete-user --user-pool-id "$POOL" --username "$WHO" --no-cli-pager 2>/dev/null
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$WHO" --message-action SUPPRESS --no-cli-pager >/dev/null || exit 1
trap 'aws cognito-idp admin-delete-user --user-pool-id "$POOL" --username "$WHO" --no-cli-pager' EXIT
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$WHO" --password "$SECRET" --permanent --no-cli-pager
TOKEN=$(aws cognito-idp admin-initiate-auth --user-pool-id "$POOL" --client-id "$CLIENT" --auth-flow ADMIN_USER_PASSWORD_AUTH \
    --auth-parameters "USERNAME=$WHO,PASSWORD=$SECRET" --query AuthenticationResult.IdToken --output text --no-cli-pager)
WHO_ID=$(aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$WHO" --query "UserAttributes[?Name=='sub'].Value" --output text --no-cli-pager)
[ ${#TOKEN} -gt 100 ] || { echo "FAIL could not sign the test person in"; exit 1; }
get() { curl -s -m 30 -b "specdev_token=$TOKEN" "$URL$1"; }
post() { curl -s -m 30 -b "specdev_token=$TOKEN" -X POST -H "Content-Type: application/json" -d "$2" "$URL$1"; }

echo "wake (instance up):   $(get /wake)"
echo "files (signed in):    $(get /api/files)"
echo "bad token:            $(curl -s -m 30 -b "specdev_token=${TOKEN}x" "$URL/api/files") / $(curl -s -m 30 -b "specdev_token=${TOKEN}x" "$URL/wake")"
echo "message:              $(post /api/message '{"text":"Write stars.bas that prints a row of 10 stars, run it and quote the report."}')"
for i in $(seq 1 60); do
    sleep 4; events=$(get "/api/events?generation=1&after=0")
    echo "$events" | grep -q '"kind": "done"\|"kind": "error"' && break
done
echo "$events" | "$PY" -c "
import json, sys
for e in json.load(sys.stdin)['events']:
    if e['kind'] in ('done', 'error'): print('agent finished:      ', e['kind'], 'error=%s' % e.get('error'), '%ss' % e.get('seconds'), '\$%s' % e.get('cost'), e.get('text', '')[:200])
    if e['kind'] == 'result' and 'report:' in (e.get('text') or ''): print('emulator said:       ', e['text'].split('report:')[-1].strip())
"
echo "screen:               $(curl -s -m 30 -b "specdev_token=$TOKEN" -o /dev/null -w '%{http_code} %{content_type} %{size_download} bytes' "$URL/api/screen.png")"
echo "files afterwards:     $(get /api/files | cut -c1-160)"

echo "--- putting the instance to sleep (as if unused for hours) ---"
remote 'touch -d "5 hours ago" /var/lib/specdev/last-active; nohup /usr/local/bin/specdev-idle-stop >/dev/null 2>&1 & sleep 1; echo asked'
for i in $(seq 1 40); do [ "$(state)" = stopped ] && break; sleep 5; done
echo "instance:             $(state)"
echo "api while asleep:     $(curl -s -m 30 -b "specdev_token=$TOKEN" -o /dev/null -w '%{http_code} after %{time_total}s' "$URL/api/files")"
echo "page while asleep:    $(curl -s -m 30 -o /dev/null -w '%{http_code}' "$URL/")"
started=$SECONDS
echo "wake:                 $(get /wake)"
for i in $(seq 1 60); do
    [ "$(curl -s -m 10 -b "specdev_token=$TOKEN" -o /dev/null -w '%{http_code}' "$URL/api/files")" = 200 ] && break; sleep 3
done
echo "back after:           $((SECONDS - started)) s, wake says $(get /wake)"
echo "files after waking:   $(get /api/files | cut -c1-160)"
[[ "$WHO_ID" =~ ^[0-9a-f-]{36}$ ]] && remote "rm -rf /var/lib/specdev/data/$WHO_ID; echo removed the test folder"
