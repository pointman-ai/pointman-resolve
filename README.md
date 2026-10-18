# resolve

A Pointman extension for DaVinci Resolve on a Mac. It renders small previews of your timelines and grabs
stills, then posts them on the board, where they can be watched and commented on from any device. Notes on
a preview come back to Resolve as markers on its timeline: purple while a note is open, green once it's
resolved. It can also render a preview by itself once the edit has changed and the Mac has been idle for a
while.

Python (standard library only), run by the node on Python 3.12. It drives Resolve through Resolve's own
scripting API.

```
pointman add resolve
```

## What it needs

- **macOS** (Apple silicon or Intel).
- **DaVinci Resolve Studio**, at `/Applications/DaVinci Resolve/DaVinci Resolve.app`, open, with
  **Preferences › System › General › External scripting using** set to **Local**. Its scripting API is
  found where Resolve installs it. The `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB` and `RESOLVE_APP`
  environment variables point at other places.
- **ffmpeg** and **ffprobe** on the PATH, to make each render's proxy, poster and thumbnail.

The node offers the commands only while Resolve is connected (`ready_when = "connected"`), so a request
made while Resolve is closed waits until it's open again.

## Commands

Agents get `resolve_render` and `resolve_still` as tools, or call any command on the Mac with
`run_on_machine`. Every Resolve call runs on one lane, one at a time, since Resolve's API isn't meant to be
driven from several threads at once.

| Kind | Params | What |
|---|---|---|
| `resolve.render` | `timeline?`, `height?` (144–2160), `thread_id?` | Renders a timeline (the open one, or one by name) to a small MP4 (H.265 where Resolve has it, else H.264) and posts it, with the timeline's layout (tracks, clips and markers), in the thread given or the project's Resolve previews thread. Takes minutes; cancellable |
| `resolve.still` | `thread_id?` | The frame under the playhead, as a PNG, posted |
| `resolve.marker` | `note`, `project`, `timeline` | Mirrors one note as a marker. `null` when that project isn't the one open, so it's tried again later |

- **Your project is left as it was:** a render saves the project's render settings to a temporary preset
  and restores them after. It also goes back to the page and timeline you were on.
- **Where it posts:** in the board project named like the Resolve project (matched against the folders
  the node allows), else in one called Resolve. A render is a `timeline` card at
  `resolve/<project>/<timeline>`, so each render of a timeline is a new version of the same card, and a
  still is at `.../frame`.
- **Disk space:** a render doesn't start with less than 10 GB free.

## By itself

- **Markers:** every 15 s it reads the notes changed on the board. Each note at a timecode on a Resolve
  preview becomes a marker at that frame, titled "Review: …" (purple) or "Resolved: …" (green), and goes
  once the note is deleted. A note whose project isn't open in Resolve waits until it is.
- **Auto renders:** with `auto_render_idle_minutes` above 0, it renders the open timeline once the edit
  has changed (clips, trims, tracks or markers) and neither it nor the Mac's keyboard and mouse have been
  touched for that long. An edit that's already been rendered isn't rendered again.

## Status

`resolve.status`, every 10 s: `connected` (and why not), Resolve's version, the page it's on, the open
project and timeline (name, fps, duration, timecode, tracks and markers), and whether it's rendering.

## Settings

Per machine, in `~/.config/pointman/extensions/resolve/config.toml`, or
`pointman config set node.extensions.resolve.<key> <value>`:

| Setting | Default | |
|---|---|---|
| `preview_height` | 720 | Previews are rendered at this height (480, 720 or 1080), or the timeline's if it's smaller |
| `auto_render_idle_minutes` | 0 | Render a preview after the edit changes and the Mac has been idle this long. 0 is off |
| `markers` | `true` | Notes on a Resolve preview become markers on its timeline |
