# Projects, the library, hand-written programs and image uploads

What the page offers beyond "type a request", and how it is stored.

## Projects

A **project** is one piece of work: its program files, its conversation with the agent, and a little metadata. Everything a person does happens in their current project.

- **New project** (replaces "New conversation") starts an empty project and makes it current. The empty chat shows **four suggestions picked at random** from a pool of about thirty; they change every time.
- A project is named after its first request (first 48 characters) until then it is "New project".
- **Opening a project** from the library restores its chat history on the page, gives the agent its memory of that conversation back (the agent session is resumed), and **loads the project's program into the emulator** (the last program that was run in it), so it is on screen ready to use or change.
- Projects cannot be switched while the agent is working (the page says so).
- The conversation is kept on disk, so it also survives a restart or deploy of the service.

On disk, per person (`<data>/<person>/`; on your own machine the person is `local`):

```
account.json  log.jsonl                 budget and the log of requests, top-ups and grants (hosted)
person.json                             address, first and last seen, suspended (hosted; see the admin page)
memory.json                             the agent's notes about the person
current                                 id of the current project
projects/<id>/project.json              name, created, updated, main (program to load), session, cost
projects/<id>/chat.jsonl                the conversation: one page event per line
projects/<id>/thumb.png                 the emulator screen when the last request finished
projects/<id>/programs/                 the agent's working folder: sources, tapes, uploads
projects/<id>/history.jsonl, history/<n>/   earlier versions of the source files (see History)
```

## The library (S3)

Hosted, every project is copied to `s3://<bucket>/library/<person>/<project id>/...` when a request finishes, when a file is saved by hand and when an image is uploaded. S3 is the library of record; the instance's disk is the working copy. If a person has no projects on the instance (a new or rebuilt instance) their library is fetched back from S3 the first time they appear.

The page's **Projects** button lists the library, newest first, as cards: screen thumbnail, name, when it was last changed, its program files and what it has cost. Clicking a card opens that project (see above), which is both "run it again" and "carry on working on it".

## Writing programs by hand

The Program pane has **New**, **Edit**, **Save & run** and **Run**:

- **New** asks for a name (`name.bas`, `.asm` or `.inc`) and opens an empty editor.
- **Edit** turns the listing into an editor; **Save & run** stores it in the project and, for a `.bas` file, runs it on the emulator. A source-format problem (missing line number, unterminated string) is shown instead of running.
- **Run** runs the shown program again.
- The agent is told, with the next request, which files were changed by hand since it last worked, so it re-reads them before changing them.
- Not available while the agent is working (one emulator per person).

## History

A version of the project's source files (`.bas`, `.asm`, `.inc`) is kept after each request to the agent and each save by hand, unless nothing changed; the last 50 are kept. The Program pane's **History** button lists them with when and why they were kept; picking one shows its files with the lines that differ from the current files marked, and **Go back to this version** puts its files back. The current files are kept as a version first, so going back can itself be undone. The agent is told, with its next request, which files changed. Not available while the agent is working.

## Share links

The Program pane's **Share** button makes a link anyone can open to play the program, without signing in: a page that runs the program's tape (as it was last run) in [JSSpeccy 3](https://github.com/gasman/jsspeccy3), a Spectrum emulator in the browser, with links to download the tape and the source. The link stays the same when it is updated after a change; **Stop sharing** takes it down. Hosted, the page, tape and source are copied to `s3://<bucket>/site/play/<id>.*` and the emulator to `site/player/` (by `deploy.py`), so a shared program plays even while the workbench is asleep. The id is random (16 characters) so links cannot be guessed; anyone who has one can pass it on.

## Uploading an image

The chat box has an **Image** button. The picture is shrunk in the browser (longest side 1568 pixels, JPEG), stored in the project as `upload-N.jpg`, shown in the chat, and the agent is told to look at it. A choice beside it says what it is:

- **Program listing** - a photo or scan of printed or hand-written program text. The agent transcribes it to a `.bas` file, keeping the line numbers, runs it, fixes what the emulator rejects, and reports anything it could not read.
- **Picture to draw** - any image. The agent writes a BASIC program that draws a line-art version with `PLOT`/`DRAW`/`CIRCLE` on the 256x176 screen, runs it, compares a screenshot with the original and refines it.
- Whatever is typed in the chat box is sent with the image as extra instructions.

Limits: PNG, JPEG, GIF or WebP; 6 MB after shrinking.

## API (all under the person's sign-in)

```
GET  /api/projects                 {current, projects: [{id, name, updated, files, cost, thumb}]}
GET  /api/thumb?id=ID              a project's thumbnail
POST /api/project/new              start a project and make it current
POST /api/project/open {id}        make a project current; restores chat, loads its program
POST /api/file {name, text}        save a source file in the current project
POST /api/run {name}               run a program from the current project now
POST /api/message {text, image?, kind?}    image = {type, data (base64)}; kind = "listing" | "picture"
GET  /api/events.stream            the page's updates; each carries {project: {id, name, share, admin}}
GET  /api/history                  {versions: [{n, at, why, files}]}, newest first
GET  /api/history/file?n=N&name=F  a file as it was in version N
POST /api/history/restore {n}      put version N back; {changed: [names]}
POST /api/share                    make or update the project's share link; {link: "/play/<id>.html"}
POST /api/share/stop               take it down
GET  /play/<id>.html, /player/...  the shared page and the emulator (served by the bucket when hosted)
```

## Not included

Renaming and deleting projects, working on a project together with another person, and more than one program "slot" per project (a project can hold several files; the one last run is the one loaded).
