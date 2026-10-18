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
aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text
aws cloudformation create-stack --stack-name specdev --template-body file://deploy/specdev.yaml --capabilities CAPABILITY_IAM --parameters ParameterKey=Subnet,ParameterValue=<public subnet> ParameterKey=Vpc,ParameterValue=<its vpc> ParameterKey=CloudFrontPrefixList,ParameterValue=<id of com.amazonaws.global.cloudfront.origin-facing> ParameterKey=Image,ParameterValue=<the image id from the first line>
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
- People are kept apart by folder checks in the server and, below those, by Landlock (`specdev/sandbox.py`, switched on by `SPECDEV_SANDBOX=1`): each person's agent, assembler and emulator can read the system's files but write, and read, only their own folders, so the assembler's `INCBIN` cannot reach another person's files or the service's settings. `setup.sh` checks the kernel supports this and stops the deploy if not; with it switched on, nothing is run unconfined. `/etc/specdev.env` is readable by root only. They still share one machine, one service user and its memory and processor, so this is not a boundary against someone attacking the kernel: invite people you trust.
- The model is Sonnet 5 because Sonnet 5.5 and Opus 5.5 are not enabled on Bedrock in this account (the call fails asking for AWS Marketplace subscribe rights). Once enabled, change `MODEL` in `deploy.py`.

## The admin page

`<Url>admin.html` (linked from the workbench's header for administrators) lists everyone: their address, whether they have signed in, when they were last seen, their projects, requests, spending and budget, and their recent requests. From there an administrator can **invite** someone (Cognito emails them a temporary password), **add credit**, and **suspend** or restore someone: that disables their Cognito sign-in and, because a sign-in lasts up to 24 hours, the server also refuses them straight away and stops their agent.

Administrators are the sign-in addresses in `deploy/admins.txt` (one per line, not checked in), or, without that file, the MCP person; `deploy.py` passes them as `SPECDEV_ADMINS`. Inviting and suspending need the instance role's Cognito permissions, added to `specdev.yaml` with the admin page. A stack created before then needs one update. Before that change the template looked up the newest Ubuntu image on every update, which would have replaced the instance and its disk; now the image is a fixed id, and the update passes the one the instance already runs. Make a change set first and check that nothing is replaced:

```
IMAGE=$(aws ec2 describe-instances --instance-ids <InstanceId output> --query "Reservations[0].Instances[0].ImageId" --output text)
aws cloudformation create-change-set --stack-name specdev --change-set-name admin --template-body file://deploy/specdev.yaml --capabilities CAPABILITY_IAM --parameters ParameterKey=Image,ParameterValue=$IMAGE ParameterKey=Subnet,UsePreviousValue=true ParameterKey=Vpc,UsePreviousValue=true ParameterKey=CloudFrontPrefixList,UsePreviousValue=true ParameterKey=InstanceType,UsePreviousValue=true ParameterKey=Port,UsePreviousValue=true
aws cloudformation describe-change-set --stack-name specdev --change-set-name admin --query "Changes[].ResourceChange.[LogicalResourceId,Action,Replacement]" --output table
aws cloudformation execute-change-set --stack-name specdev --change-set-name admin     # only if the table shows Role Modify and no replacement
aws cloudformation wait stack-update-complete --stack-name specdev
```

## Share links

Shared programs (see `specdev/PROJECTS.md`) are written to `site/play/` in the bucket and served by CloudFront like the page, so they work while the instance is off and need no sign-in. `deploy.py` puts the browser emulator they play in (JSSpeccy 3.2, GPL-3, checked against a pinned SHA-256) in `site/player/` on every deploy.

## MCP server (for Claude, ChatGPT and other assistants)

`specdev/mcp.py` lets an assistant use the workbench as a tool: `list_projects`, `open_project`, `new_project`, `build` (ask the agent for a program), `check` (progress, the screen as an image, download links) and `share` (a public link to play the program). The address is `<Url>mcp/<key>`; the key is made on the first deploy and kept in `deploy/mcp-key.txt`. Everything is done as one person, on their budget, so treat the address as a password: their sign-in address goes in `deploy/mcp-person.txt` (or `$SPECDEV_MCP_PERSON`); neither file is checked in. To change the key, delete `mcp-key.txt` and deploy again; to switch the server off, delete `mcp-person.txt` and deploy.

It is the simplest form of MCP's HTTP transport (one JSON answer per request, no sign-in flow). `build` waits up to 40 seconds and otherwise tells the assistant to call `check`.

`build`, `check` and `open_project` also name an interactive view (an MCP App, `specdev/static/widget.html`): hosts that support MCP Apps show the live Spectrum screen with sound, a download button for the tape and a link to the website inside the chat; other hosts get the screen as an image. The view reads the screen and sound straight from `/mcp/<key>/...`, which is why those paths allow any origin. To try it in Claude, add a custom connector (Settings, Connectors) with the MCP address. The view has been checked against the MCP Apps spec in a stand-in host, not yet inside Claude or ChatGPT.
