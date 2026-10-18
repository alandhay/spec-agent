# Hosting specdev on AWS

One Linux instance runs the server, an agent process and a headless Fuse per signed-in person; CloudFront is the only way in; Cognito handles sign-in; the agent reaches Claude through Bedrock with the instance's role (no keys stored). The instance switches itself off after two hours unused and the page starts it again.

```
Browser -> CloudFront (https)
             /            -> S3 site/         the page: loads even while the instance is off
             /api/config  -> S3 site/         sign-in settings, written by deploy.py
             /wake        -> Lambda           is the instance up? start it if not (signed-in people only)
             /api/*       -> EC2 t4g.medium, port 8766 (reachable from CloudFront only)
                               specdev server
                               per person: agent + Xvfb + Fuse + /var/lib/specdev/data/<id>/programs
                               -> Bedrock (Claude)
Cognito: sign-in (invited people only)        S3: also the code bundle and a copy of everyone's programs
```

"Unused" means no message sent and no agent working; an open tab alone does not keep it up. `/usr/local/bin/specdev-idle-stop` (cron, every five minutes) copies the programs to the bucket and powers off. Opening the page, or sending a message from a page that says "Asleep", wakes it: about 20 seconds.

## First time

From the project root, with the AWS CLI signed in (region us-east-1):

```
aws cloudformation create-stack --stack-name specdev --template-body file://deploy/specdev.yaml --capabilities CAPABILITY_IAM --parameters ParameterKey=Subnet,ParameterValue=<public subnet> ParameterKey=Vpc,ParameterValue=<its vpc> ParameterKey=CloudFrontPrefixList,ParameterValue=<id of com.amazonaws.global.cloudfront.origin-facing>
aws cloudformation wait stack-create-complete --stack-name specdev
python deploy/deploy.py
aws cognito-idp admin-create-user --user-pool-id <UserPoolId output> --username someone@example.com
```

The last line emails that person a temporary password; repeat it for each person you invite. The address of the site is the stack's `Url` output.

## Afterwards

```
python deploy/deploy.py                                   # put the current code on the instance and restart
python deploy/deploy.py run "journalctl -u specdev -n 50"  # run a command on the instance (no SSH is open)
bash deploy/check.sh                                      # outside-in test as a throwaway person: agent run, sleep, wake (a few cents; stops the instance for a minute)
aws cloudformation delete-stack --stack-name specdev      # take it all down (the bucket is kept)
```

Both `deploy.py` commands start the instance first if it is off. Settings live in `deploy.py` (model, starting budget per person, minutes before switching off) and are written to `/etc/specdev.env` on each deploy. Sessions with no activity for 30 minutes are put to sleep (emulator and agent process stopped); the conversation resumes on the next message.

## Limits

- One instance: about 6 requests are worked on at once (`SPECDEV_MAX_RUNNING`), and it is a single point of failure.
- People are kept apart by folder checks in the server, not by separate machines; the assembler's `INCBIN` can still read any file the service user can. Invite people you trust.
- The page still polls rather than streaming.
- The model is Sonnet 5 because Sonnet 5.5 and Opus 5.5 are not enabled on Bedrock in this account (the call fails asking for AWS Marketplace subscribe rights). Once enabled, change `MODEL` in `deploy.py`.

## MCP server (for Claude, ChatGPT and other assistants)

`specdev/mcp.py` lets an assistant use the workbench as a tool: `list_projects`, `open_project`, `new_project`, `build` (ask the agent for a program) and `check` (progress, the screen as an image, download links). The address is `<Url>mcp/<key>`; the key is made on the first deploy and kept in `deploy/mcp-key.txt`. Everything is done as one person, on their budget, so treat the address as a password: their sign-in address goes in `deploy/mcp-person.txt` (or `$SPECDEV_MCP_PERSON`); neither file is checked in. To change the key, delete `mcp-key.txt` and deploy again; to switch the server off, delete `mcp-person.txt` and deploy.

It is the simplest form of MCP's HTTP transport (one JSON answer per request, no sign-in flow, no interactive widget). `build` waits up to 40 seconds and otherwise tells the assistant to call `check`.
