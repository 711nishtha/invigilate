# Proctoring

## 1. What this is, and what it is not

Phase 5 adds a monitored exam mode: a pre-exam security check, camera and
optional microphone/screen monitoring during the exam, evidence a human can
look at, and a teacher review surface.

**What it provides.** Deterrence against opportunistic misconduct, a timeline of
what the browser observed, evidence attached to specific moments, and a
server-side decision about whether an exam may begin at all.

**What it does not provide.** A browser cannot see the room, a phone on the
desk, a second laptop, a person off-camera, a virtual machine, a second monitor,
or another application. It cannot prevent a screenshot. It cannot keep running
once the tab closes. Anyone telling you otherwise about a web-based proctoring
product is selling something.

This distinction is not a disclaimer bolted onto the end. It shapes the schema
(`ProctoringStatus` has no `PASSED`), the vocabulary (`ProctoringReview` has no
`CHEATING`), the UI (caveats sit above the numbers) and the tests
(`TestNoOverstatementInSource` makes overstatement a build failure).

## 2. The governing rule

```
signal -> evidence -> policy -> teacher review
```

and never `signal -> accusation`. Every stage is separate on purpose:

- a **signal** is something a browser reported;
- **evidence** is an artefact a human can look at and disagree with;
- **policy** is a server-side rule a teacher configured;
- **review** is a person's decision, attributed to them.

Nothing in the platform converts a count of signals into a judgement about a
person, and there is no field anywhere that could store one.

## 3. Modes

| Mode | Pre-exam check | Monitoring | Status |
|---|---|---|---|
| `STANDARD` | none | Phase 3 signals only (tab, focus, clipboard) | works |
| `PROCTORED` | required | camera, optional mic/screen, face presence | works |
| `LOCKDOWN` | required | OS-level lockdown | **refused — no client exists** |

`proctoring_mode` is a new column, orthogonal to `security_level`.
`security_level` governs in-page behaviour (fullscreen, copy/paste);
`proctoring_mode` governs whether the server will start the exam at all.

**LOCKDOWN is refused, not downgraded.** Opening a precheck for a LOCKDOWN quiz
returns `LOCKDOWN_CLIENT_REQUIRED` and no attempt is ever created. A real
desktop client would attach at `app/lockdown/desktop.py`; until one exists and
has been tested, running such an exam as PROCTORED would hand a teacher a
guarantee they do not have. See [§11](#11-lockdown-and-the-desktop-client).

## 4. Server-side exam start authorization

This is the security-critical piece of the phase.

```
POST /public/quizzes/{code}/precheck      -> PENDING   no attempt, no clock
POST /public/precheck/{id}/capabilities   -> PENDING | BLOCKED
POST /public/precheck/{id}/readiness      -> READY | FAILED
POST /public/precheck/{id}/start          -> CONSUMED, attempt created
```

Four properties make this an authorization boundary rather than a wizard:

**The browser cannot start a proctored exam.**
`POST /public/quizzes/{code}/attempts` — the Phase 3 route — refuses any quiz
that requires a check, with `PRECHECK_REQUIRED`. There is no header, flag or
payload field that relaxes it. There is no second door.

**No clock runs during the check.** The attempt does not exist yet. A student
who spends four minutes on camera permissions loses none of their exam time,
and there is no half-started attempt in a paused state for the finalization path
to learn about. `create_attempt` is Phase 3 code, unmodified, and it assigns
`started_at` from the server clock at the moment it runs — which is now the
moment the server authorizes the start.

**The client cannot assert a verdict.** `ReadinessReport` has no `ready` field,
no `status`, no `severity` and no `passed`. `StrictModel` forbids extra keys, so
sending one is a 422 rather than something quietly discarded. The only thing
that can produce `ready=True` is `evaluate_readiness` on the server.

**A stored `READY` is not a licence.** `authorize_start` re-evaluates the policy
against the stored observations before creating anything. A teacher who tightens
the policy while a student sits on the readiness screen gets the stricter
policy; a bug that somehow wrote `READY` without the observations still does not
produce an exam.

The session is single-use, 15-minute TTL, resolved token-first with a uniform
404 — the same shape as attempt tokens, for the same reason: the endpoint must
not become an existence oracle.

## 5. Capability detection blocks, it never downgrades

The browser reports what it can do; the server compares that with the policy. A
browser that falls short is **blocked** with a named reason.

This is the single most damaging thing this layer could get wrong. A quietly
reduced exam means the teacher sees "PROCTORED" in the results with no way to
know that this particular student's browser never ran a camera.

The report is untrusted, but it can only be used to *deny*: a browser that
claims less than it has is refused, and one that claims more still has to
produce the actual permission grants and signals afterwards. Lying buys nothing.

### Face detection is Chromium-only, and that is reported honestly

`FaceDetector` (the Shape Detection API) exists in some Chromium builds and
nowhere else — not Firefox, not Safari, not every Chrome.

There is **no polyfill and no bundled model**. Shipping several megabytes of ML
weights into an exam page would cost every student battery and framerate, and
faking detection without one would be worse than not having it. So the
capability is detected, reported, and a browser without it cannot sit an exam
that requires face presence. The student is told that Chrome or Edge on a
desktop would work.

## 6. During the exam

A heartbeat at the cadence **the server** sets (default 30s) reports observed
stream state. The server records signals, updates `proctoring_status`, and
applies the configured stream-loss action.

### Missing heartbeats are caught where a client cannot avoid them

A client can simply stop calling the heartbeat endpoint. So
`check_heartbeat_staleness` runs on the *ordinary* student request path — every
state poll, every answer save. A student answering questions while their
monitoring loop is dead is still caught.

Silence marks the attempt `UNVERIFIABLE`. It does not end the exam: a closed
laptop and a dead router look identical from the server, and ending an exam on
that ambiguity punishes the wrong person often enough to be unacceptable.

### One frame is never a decision

Every counted proctoring signal has a **repeat window** (`REPEAT_WINDOW_SECONDS`)
enforced server-side. A second occurrence inside the window is recorded for the
timeline at severity zero and marked `repeat_suppressed`.

This exists because the client is the thing it protects against. A webcam that
blinks, a student who leans out of frame, or a client stuck in a loop would
otherwise walk an honest attempt into auto-submission. A test fires 40
`FACE_MISSING` events and asserts the total is 2.

### Unknown is not absent

When the face detector returns nothing usable, the client reports
`monitoring_impaired` — **worth zero severity** — rather than `FACE_MISSING`.
Punishing honest self-reporting would train clients to stop doing it, and that
signal is the only one that says the green ticks are meaningless. It changes
`proctoring_status` instead, which is what a teacher acts on.

### Severity weights

| Signal | Weight | Why |
|---|---|---|
| `PROCTORING_UNAVAILABLE` | 0 | Honest self-reporting must be free |
| `FACE_PRESENT`, `*_STARTED` | 0 | Timeline context |
| `GAZE_AWAY_SUSTAINED` | 1 | Looking away is what people do when they think |
| `DEVICE_CHANGED` | 1 | Weak alone, meaningful in aggregate |
| `FACE_MISSING` | 2 | A required condition was not observed |
| `OBJECT_DETECTED` | 2 | Same, and no detector currently produces it |
| `PROCTORING_HEARTBEAT_LOST` | 2 | The server could not see |
| `CAMERA_STOPPED`, `MICROPHONE_STOPPED` | 3 | A stream the student agreed to keep running stopped |
| `*_PERMISSION_DENIED` | 3 | A permission required to sit the exam was refused |
| `MULTIPLE_FACES_DETECTED` | 3 | The strongest signal a webcam can give — and still only a signal |

### ⚠ The default integrity budget is smaller than a stopped stream

`CAMERA_STOPPED` is worth **3**; `max_integrity_violations` defaults to **2**.

**A PROCTORED quiz left at the default ends the exam the first time a camera
drops**, whatever `on_stream_loss` says, because the integrity budget fires
first. That is a configuration choice a teacher should make deliberately, so it
is documented here and pinned by a test rather than quietly softened.

If you want a camera drop to warn rather than end the exam, set
`max_integrity_violations` to at least 4 when creating the quiz.

### Stream loss

`on_stream_loss` is `WARN` (default), `FLAG` or `AUTO_SUBMIT`. The default does
not end an exam because a webcam driver crashed. When an exam *is* ended, it
goes through the same Phase 3 `finalize_attempt` path with the same grading:
a proctoring decision can end an exam early, but it cannot change a mark.
Nothing in `proctoring_service.py` writes to `score`, `percentage` or
`is_correct`.

## 7. Evidence and privacy

Evidence is what makes a signal reviewable. "No face detected at 14:32" is an
assertion by an untrusted browser; a still image at 14:32 is something a human
can look at and disagree with.

- **The bucket is private.** Nothing produces a public URL. Teachers read
  through a 5-minute signed URL minted *after* an ownership check.
- **Stills by default.** Continuous recording is off. Active clip recording is
  not shipped at all — see [§8](#8-what-is-deliberately-not-implemented).
- **Retention is written at capture**, from the quiz policy, so nothing is
  stored without a deletion date.
- **The student never reads it back.** The upload response has no URL and no
  path field.
- **The path is never logged**, and `object_path`/`signed_url` are redaction
  keys so an accidental log is harmless too.
- **A storage failure never costs an exam.** Capture returns rather than raises;
  the signal survives, the picture does not.
- **Retention is real.** `scripts/purge_evidence.py` deletes the object *then*
  the row — an orphaned row is recoverable, an untracked object in a bucket is
  not. `StorageProvider.delete` is part of the interface precisely so that
  "kept for 30 days" cannot silently mean "kept forever, with a date attached".

Run the purge daily:

```bash
python -m scripts.purge_evidence            # delete what is due
python -m scripts.purge_evidence --dry-run  # report only
```

Exit 0 = clean, 1 = some deletions failed, 2 = could not run.

### Size limits

Evidence uploads are capped by the smaller of the declared limit and
`MAX_REQUEST_BYTES` (256 KB by default). A consequence worth stating: **clip
upload is unreachable at the default setting**, which is consistent with clip
recording not being shipped. Enabling it would require raising
`MAX_REQUEST_BYTES` deliberately rather than discovering the ceiling in
production.

### Consent

The notice is shown and accepted **before** `getUserMedia` is ever called. It
names what is captured, how long it is kept, and who can see it, and it includes
the lockdown provider's own list of what monitoring *cannot* do, rendered
verbatim rather than paraphrased upward.

This platform makes **no legal compliance claim**. It does not assert GDPR,
FERPA or any other framework. It gives you consent capture, a stated retention
period that is actually enforced, private storage and an audit trail; whether
your use of those satisfies your jurisdiction and your institution's policy is a
question for your institution, not for this document.

## 8. What is deliberately not implemented

Listed plainly, because a gap you know about is manageable and one you discover
in an exam is not.

| Feature | Status | Why |
|---|---|---|
| Object / phone detection | **Architecture only** | No browser API does this. Event type, severity, evidence path and review surface all exist; no model is shipped, and the capability reports `false`. |
| Screen recording | **Capability detection only** | `MediaRecorder` is detected but never started. Stills tied to events cost a fraction of the privacy of filming a room for an hour. |
| Gaze tracking | **Event type only** | No signal currently produces `GAZE_AWAY_SUSTAINED`. It is weighted at 1 so that if a detector is added, it is already treated as the weak signal it is. |
| OS-level lockdown | **Refused** | Needs a signed desktop client. None exists. |
| Browser/VM/extension detection | **Not attempted** | It does not work, and the attempts that exist mostly flag unusual-but-honest setups. |

## 9. Configuration

Set `proctoring_mode` and `proctoring_config_json` on the quiz. Both are in
`EXAM_SHAPE_FIELDS`, so they cannot change while a quiz is published.

An unrecognised key is **refused**, not stored and ignored — the same rule as
`coverage_config_json`, for the same reason: a setting nothing reads is worse
than no setting. Cross-field rules are checked too, so `require_face_presence`
without `require_camera` is rejected rather than producing an exam that reports
nothing and looks configured.

| Key | Default (PROCTORED) | Range |
|---|---|---|
| `require_camera` | `true` | bool |
| `require_microphone` | `false` | bool |
| `require_screen_share` | `false` | bool |
| `require_fullscreen` | `true` | bool |
| `require_face_presence` | `true` | bool |
| `detect_multiple_faces` | `true` | bool |
| `detect_gaze` | `false` | bool (no detector shipped) |
| `detect_objects` | `false` | bool (no detector shipped) |
| `record_screen` | `false` | bool (not shipped) |
| `require_consent` | `true` | bool |
| `store_evidence` | `true` | bool |
| `evidence_retention_days` | `30` | 1–365 |
| `face_missing_grace_seconds` | `15` | 5–300 |
| `gaze_away_grace_seconds` | `20` | 5–300 |
| `heartbeat_interval_seconds` | `30` | 10–300 |
| `heartbeat_grace_seconds` | `120` | 30–900 |
| `on_stream_loss` | `WARN` | `WARN`/`FLAG`/`AUTO_SUBMIT` |
| `allow_mobile` | `false` | bool |

## 10. Browser compatibility

| Browser | Camera/mic | Screen share | Fullscreen | Face detection |
|---|---|---|---|---|
| Chrome (desktop) | yes | yes | yes | **usually** — build-dependent |
| Edge (desktop) | yes | yes | yes | **usually** — build-dependent |
| Firefox | yes | yes | yes | **no** |
| Safari | yes | limited | yes | **no** |
| Mobile browsers | yes | mostly no | limited | no |

A quiz requiring face presence is therefore effectively Chrome/Edge desktop
only, and a student on Firefox is told so clearly at the precheck rather than
discovering it mid-exam. Set `require_face_presence: false` if you need wider
browser support — accepting, knowingly, that the exam then verifies only that a
camera is running.

`allow_mobile` defaults to `false`. Everything requires a secure (https)
context; `getUserMedia` does not exist without one.

## 11. LOCKDOWN and the desktop client

`app/lockdown/base.py` states what a browser cannot do, in a docstring, at the
top of the module, so that the next person to work here reads it first.

`DesktopLockdownProvider` reports **no** capabilities and raises
`LockdownClientUnavailable` from `verify_client`. Both are deliberate:

- returning the capabilities a future client *would* have would put "blocks
  other applications" in front of a teacher today;
- accepting any attestation would mean a web page could unlock LOCKDOWN mode by
  claiming to be the desktop client — which is exactly the claim an attacker
  would make, and a browser cannot prove it.

A real client would need: a signed build per platform, an attestation the server
verifies against a public key, a process allowlist, display enumeration, and a
tested uninstall path. **Do not enable LOCKDOWN mode until that exists and has
been tested.** The server currently refuses it, which is the correct behaviour
and not a bug to route around.

## 12. Verification status

| Check | Status |
|---|---|
| Backend test suite | **749 passing**, 0 failing (SQLite) |
| Phase 5 tests | 166 new (583 -> 749) |
| Phase 1–4 regression | **passing**, unchanged |
| Frontend typecheck + build | **passing** |
| Migration 0004 renders for PostgreSQL (offline) | **passing**, both directions |
| Migration/model enum parity | **passing**, asserted by test |
| Migration executed against live PostgreSQL | **NOT RUN** — no PostgreSQL reachable in this environment (Docker daemon not running, nothing on :5432) |
| Concurrency tests (row locking) | **NOT RUN** — PostgreSQL-only, same reason |
| Supabase Storage evidence upload | **UNVERIFIED** — no credentials |
| Real camera / face detection in a browser | **NOT TESTED** — needs a human at a machine with a webcam |

The last three are the honest gaps. Face detection in particular has been
implemented against the documented `FaceDetector` API and is capability-gated,
but it has not been exercised against a real webcam in a real Chrome build.
Before a first proctored exam, run one yourself end to end.
