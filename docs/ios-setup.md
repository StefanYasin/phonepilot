# iOS setup

The board is only half of PhonePilot. This document builds the other half: two
Shortcuts and one automation that let the phone do app level work the board
cannot do.

Time needed: about ten minutes, plus the time it takes you to decide which
photos belong in your queue.

## What you are building

| Piece | Job |
|---|---|
| `PhonePilot Sync` | Polls the board, takes one job, routes it, asks you to confirm posts, reports the result. |
| `PhonePilot Queue Check` | Counts the photos in your queue album. Used by the flagship template. |
| Personal Automation | Runs `PhonePilot Sync` when the board connects over Bluetooth. |

The board never posts anything. It queues a job and, when you press its button,
arms an approval window. The phone Shortcut does the visible work.

## 1. Flash and power the board

Follow [firmware/README.md](../firmware/README.md). Before you continue:

- `config.h` has your Wi-Fi name, password, and a long `API_TOKEN`.
- The board is powered over USB.
- The serial monitor at 115200 baud prints its IP address. Write that down and
  reserve it in your router so it does not change.

Test from a computer on the same network:

```bash
export TOKEN='the-value-from-config.h'
curl -s -H "X-PhonePilot-Token: $TOKEN" http://192.168.1.50/api/v1/status
```

You should see JSON with `"ok": true`, `"firmware": "0.1.1"`, and
`"ble_connected": false`. That field becomes true after step 2, and
`"ble_paired"` becomes true once iOS has finished encrypting the link.

If that fails, fix it before touching Shortcuts. Shortcuts is a bad debugger.

## 2. Pair the board over Bluetooth

1. Open Settings, then Bluetooth.
2. Turn Bluetooth off and on once, so the list is fresh.
3. Tap `PhonePilot` under Other Devices.
4. There is no PIN. The board is a keyboard, and iOS pairs it as one.

Verify with `curl` again. `"ble_connected"` and `"ble_paired"` should now be
true.

If `PhonePilot` never appears:

- Confirm the sketch uses `HijelHID_BLEKeyboard` and that the board has BLE.
  A plain ESP32 does. The ESP32-S2 and ESP32-P4 do not.
- Open the serial monitor. The sketch prints a waiting message every 30 seconds.
- Remove any old pairing on the phone and reboot the board.

## 3. Build `PhonePilot Queue Check`

This one is short. It exists so the flagship template has a real first step and
so you can see the queue size without opening the Photos app.

1. Open Shortcuts, tap `+`, name it `PhonePilot Queue Check`.
2. Add `Find Photos`.
   - Filter: Album is `PhonePilot Queue`.
   - Sort by: Date Created, Oldest First.
   - Limit: 1.
3. Add `Count`.
   - Input: Photos.
4. Add `Show Notification`.
   - Text: `Photos ready: ` then the Count variable.

Create the album `PhonePilot Queue` in Photos and put one photo in it before
you test. The sample image in `phonepilot/templates/media/queue/sample.png` is a
placeholder for the repository, not a photo on your phone.

## 4. Build `PhonePilot Sync`

This is the important one. Create a new Shortcut named `PhonePilot Sync`.

### 4.1 Ask the board for work

1. Add `Get Contents of URL`.
   - URL: `http://192.168.1.50/api/v1/next`
   - Method: GET
   - Headers: `X-PhonePilot-Token` = the token from `config.h`.
2. Add `Get Dictionary Value`.
   - Get: Value for key `job` in `Contents of URL`.
   - Save the output as `Job`.
3. Add `If`.
   - Input: `Job`.
   - Condition: has any value.

Everything below goes inside the `If` block. When the queue is empty, the
Shortcut does nothing and stops.

### 4.2 Route by job type

4. Add `Get Dictionary Value`.
   - Get: Value for key `type` in `Job`.
   - Save as `Job Type`.
5. Add `Choose from Menu` with the prompt `PhonePilot job`.
   - Menu item: `shortcut`
   - Menu item: `social_post`

Put the next actions under the matching menu item.

### 4.3 The `shortcut` branch

6. Under `shortcut`, add `Get Dictionary Value`.
   - Get: Value for key `name` in `Job`.
   - Save as `Job Name`.
7. Add `Run Shortcut`.
   - Shortcut: `Job Name` (the variable, not a fixed shortcut).
   - Turn off `Show in Share Sheet`.

This is how a workflow's `trigger_shortcut` step reaches any Shortcut you have.
No URL scheme typing, no guessing which app has focus. Shortcut jobs are one
shot: the board removes one as soon as it hands it over, so this branch does not
report a result. Social post jobs stay in the queue until you report back.

### 4.4 The `social_post` branch

This branch prepares a post and asks you twice: once before it opens anything,
once after you say you published. It never publishes for you.

6. Add `Get Dictionary Value` three times, all reading from `Job`:
   - `platform` into `Platform`
   - `caption` into `Caption`
   - `id` into `Job ID`
7. Add `Choose from Menu` with the prompt `Post to Platform?`.
   - `Post now`
   - `Skip this one`

Under `Skip this one`:

8. Add `Get Contents of URL`.
   - URL: `http://192.168.1.50/api/v1/result`
   - Method: POST
   - Headers: `X-PhonePilot-Token`, and `Content-Type` = `application/json`
   - Request Body: JSON with `job_id` = `Job ID`, `status` = `skipped`,
     `detail` = `operator skipped`.
   This puts the job back in the board queue so it is not lost.

Under `Post now`:

9. Add `Find Photos`.
   - Filter: Album is `PhonePilot Queue`.
   - Sort by: Date Created, Oldest First.
   - Limit: 1.
10. Add `Copy to Clipboard`.
    - Input: `Caption`.
11. Add `Share`.
    - Input: the photo from step 9.
    - This opens the iOS share sheet. Pick Instagram. The photo is attached,
      the caption is on your clipboard, and you finish the post by hand.
12. Add `Choose from Menu` with the prompt `Did you publish it?`.
    - `Yes, report it`
    - `No, put it back`

Under `Yes, report it`:

13. Add `Get Contents of URL`.
    - URL: `http://192.168.1.50/api/v1/result`
    - Method: POST
    - Headers: token and `Content-Type: application/json`
    - Request Body: JSON with `job_id` = `Job ID`, `status` = `posted`,
      `detail` = `published from iPhone`.

Under `No, put it back`: repeat the same `result` call with `status` =
`skipped`.

Why the share sheet and not a direct post: Instagram has no public posting API
for personal accounts. Any tool that claims otherwise is either using a
business account's Graph API or automating the UI in a way that breaks often.
PhonePilot chooses the boring, durable path: prepare everything, hand it to the
share sheet, let you tap Post.

If you manage a business account with Graph API access, you can put that call
in an `api_call` step in the workflow and skip this branch. That is outside
v0.1.x.

### 4.5 Optional: drain more than one job

The Shortcut handles one job per run. To drain up to five:

1. Wrap the whole body in `Repeat 5 times`.
2. At the end of the `If` block, add `Otherwise` and `Exit Shortcut` inside
   the repeat when the queue is empty.

Keep the limit. An unbounded loop on a phone is how you end up with a hundred
posts and a dead battery.

## 5. Trigger the Shortcut

### Manual

- Add `PhonePilot Sync` to the Home Screen.
- Or assign it to Back Tap: Settings, Accessibility, Touch, Back Tap.
- Or assign it to the Action Button on newer iPhones.

### Automatic, when the board connects

1. Shortcuts, Automation, `+`.
2. Choose `Bluetooth`.
3. Device: `PhonePilot`.
4. Run: `PhonePilot Sync`.
5. Turn off `Ask Before Running` if iOS offers the toggle.

This fires when the board powers on or reconnects, which is a useful physical
"start the queue" gesture. It does not fire when the phone merely comes into
range of an already connected board. Power cycle the board or run the Shortcut
manually.

### Automatic, on a schedule

iOS personal automations can run at a time of day. That is a reasonable second
trigger, but remember that autorun on the Python side and autorun on the phone
are separate. The Python scheduler decides when to queue a job. The phone
decides when to pick it up.

## 6. Test the whole path

From the computer:

```bash
phonepilot workflows run photo_post_queue --device iphone-01 --dry-run
phonepilot workflows run photo_post_queue --device iphone-01
```

The second command asks in the terminal. Say yes. Then run `PhonePilot Sync` on
the phone. You should see:

1. The queue check shortcut notification.
2. The `Post to instagram?` menu.
3. The share sheet with your photo and the caption on the clipboard.
4. After you publish, the second menu, then a `result` POST.

Check the board:

```bash
curl -s -H "X-PhonePilot-Token: $TOKEN" http://192.168.1.50/api/v1/status
```

`queue` should be 0 and `last_result` should read `posted: published from
iPhone`.

## 7. The button

- Short press: arms a 90 second approval window and lights the LED.
- Long press, 1.2 seconds or more: clears the queue, disarms approval, sends
  Escape to the phone.

The approval flag is informational in v0.1.x. `PhonePilot Sync` still shows its
menu unless you edit it. That is intentional: a physical button is convenient,
not a substitute for reading the caption you are about to publish.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 bad or missing token` | Header missing or wrong | Copy `API_TOKEN` exactly; no trailing space. |
| `409 no BLE host connected` | Phone not paired, or Bluetooth off | Settings, Bluetooth, tap `PhonePilot`. |
| `503 job queue is full` | Eight jobs waiting | Run Sync or long press the button to clear. |
| Shortcut cannot reach the board | Different network, or Local Network permission denied | Put the phone on the same Wi-Fi. Check Settings, Privacy and Security, Local Network. |
| `Get Contents of URL` returns text, not a dictionary | Wrong URL or an error page | Open the URL in Safari; compare with the curl output. |
| Nothing happens on Bluetooth connect | Automation is off, or the board was already connected | Power cycle the board; check Automation settings. |
| Post opens the wrong app | Share sheet order changed | Pick Instagram explicitly in the share sheet. |

## Limits worth knowing

- The phone and the board must be on the same LAN. There is no cloud relay.
- The board serves plain HTTP. Do not port forward it.
- Shortcuts cannot read a file path from your computer. The `media` field in a
  template is a record and a hint. The real photo comes from the phone album.
- A Shortcut cannot reliably tap Post inside another app. That is why the last
  step is yours.
- iOS may pause long Shortcuts in the background. Keep `PhonePilot Sync` short.
