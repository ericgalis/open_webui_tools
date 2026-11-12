## Goal
One or two sentences: what changes for the user when this is done. Written so that someone who has not seen the chat understands it.

## Requirements
- What the change must do, one requirement per line, each short enough to check.
- Name files, stacks and existing behaviour you have read. Never state a version number or URL from memory: say "the newest release at least two weeks old" and let the build look it up.

## Security requirements
- Exposure: who can reach it (this host only, the LAN, Tailscale, the internet) and through which network.
- Authentication: how a caller proves who they are, or "none, because ...".
- Data: what it stores or handles and how sensitive that is.
- Secrets: which secrets it needs, where they live (the central `.env`), and that none go into the repo or the image.
- Least privilege: what it may touch, what it must not (user, capabilities, mounts, networks).

## Acceptance checks
Written as end-to-end checks that run against the stack in the dev sandbox (`<stack>/test/e2e.sh`), each one able to fail before the change exists.
- [ ] A command or request, and the exact result that means it works.
- [ ] A command or request, and the exact result that means a misuse is refused.

## Stacks affected
- `stack_name`: why it changes. Every stack that must be deployed after this is merged.

## Not in scope
- What someone might expect but this Feature does not do.

## Tasks
- [ ] One small step the coder can finish in one session, with the files it touches.
