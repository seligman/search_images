/* Manage page: the libraries (add, edit, remove, scan, describe), indexed
   data that has lost its library, and the jobs with their log.

   Everything comes from the admin API (admin.py) through SHELL.admin. The
   jobs are polled every second while one runs or waits, and every 10
   seconds otherwise; the library cards are refreshed when a job starts or
   finishes, and every 10 seconds while one runs. Parts of the page are
   rebuilt only while no mouse button is down, so a refresh never swallows
   a click.

   ?dialog=add opens the Add dialog, ?dialog=browse the folder picker, and
   ?dialog=edit|remove|failures|export&id=<library id> those dialogs (with
   &step=review the Export dialog's review step, with &step=done the result
   of the library's last export), ?dialog=trip-export&id=<trip id> the Export
   dialog of a trip (trip_export.py; the trip page's Export button), so they
   can be linked to. Nothing in the address ever makes the server look at a folder by
   itself or start a job: the folder picker always starts at the drives,
   and an export only starts with its button.
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var J = SHELL.jobs;
    var R = EXPORT_REVIEW;

    var POLL_ACTIVE = 1000;
    var POLL_IDLE = 10000;
    var STATE_REFRESH = 10000;
    var STATE_RETRY = 10000;
    var LOG_LIMIT = 1000;
    var REVIEW_PAGE = 60;       // pictures of a group the export review shows at first
    var COUNT_DELAY = 400;      // ms after a change before the export count is asked again
    var CLOUD_OK_KEY = "searchImages.cloudOk.";
    var TEST_COST_KEY = SHELL.TEST_COST_KEY;
    var field = SHELL.field;
    var notice = SHELL.notice;
    var warningList = SHELL.warningList;
    var money = fmt.money;
    var PATH_PLACEHOLDER = "C:\\Photos\\Family  or  \\\\server\\share\\Photos";

    var state = null;           // the last /state reply
    var snapshot = null;        // the last jobs snapshot
    var logLines = [];
    var logSeq = 0;
    var logFilter = "all";
    var pollTimer = null;
    var lastStateAt = 0;
    var jobKey = null;          // changes when a job starts, finishes or is queued
    var stateRequest = null;
    var statusRetries = 0;
    var activityNodes = {};     // library id -> its card's activity box
    var pointerDown = false;
    var deferred = {};          // renders waiting for the mouse button to be released
    var currentView = null;     // {key, update(job)} of the current-job box
    var listKeys = {};
    var snapshotListeners = []; // functions called with every jobs snapshot

    function byId(id) {
        return document.getElementById(id);
    }

    /* ---------- Rendering without swallowing clicks ------------------------ */

    function whenIdle(name, render) {
        if (pointerDown) {
            deferred[name] = render;
        } else {
            render();
        }
    }

    document.addEventListener("mousedown", function () {
        pointerDown = true;
    }, true);
    document.addEventListener("mouseup", function () {
        pointerDown = false;
        // After the click that this mouseup completes has been handled.
        window.setTimeout(function () {
            var pending = deferred;
            deferred = {};
            Object.keys(pending).forEach(function (name) {
                pending[name]();
            });
        }, 0);
    }, true);

    function focusKey(container) {
        var active = document.activeElement;
        if (active && container.contains(active) && active.getAttribute("data-key")) {
            return active.getAttribute("data-key");
        }
        return null;
    }

    function restoreFocus(container, key) {
        if (!key) {
            return;
        }
        var nodes = container.querySelectorAll("[data-key]");
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute("data-key") === key) {
                nodes[i].focus();
                return;
            }
        }
    }

    /* ---------- Small builders ------------------------------------------- */

    function button(text, className, onClick, props) {
        var node = el("button", {type: "button", className: "button " + (className || ""),
                                 text: text});
        Object.keys(props || {}).forEach(function (key) {
            if (key === "key") {
                node.setAttribute("data-key", props.key);
            } else if (props[key] !== undefined) {
                node[key] = props[key];
            }
        });
        if (onClick) {
            node.addEventListener("click", onClick);
        }
        return node;
    }

    function progressBar(value, options) {
        options = options || {};
        var bar = el("div", {className: "progress-bar"});
        var box = el("div", {className: "progress", attrs: {role: "progressbar",
                                                            "aria-label": options.label,
                                                            "aria-valuemin": "0",
                                                            "aria-valuemax": "100"}}, [bar]);
        setProgress(box, value, options);
        return box;
    }

    // value: 0..100, or null for "working, amount unknown".
    function setProgress(box, value, options) {
        options = options || {};
        var bar = box.firstChild;
        box.classList.toggle("indeterminate", value === null && !options.still);
        box.classList.toggle("done", value === 100 && Boolean(options.doneColor));
        if (value === null) {
            bar.style.width = options.still ? "0" : "";
            box.removeAttribute("aria-valuenow");
        } else {
            bar.style.width = value + "%";
            box.setAttribute("aria-valuenow", String(value));
        }
    }

    // A notice above the libraries that stays until it is dismissed.
    function showNotice(kind, content) {
        var holder = byId("manage-notices");
        var close = button("Dismiss", "small");
        var box = notice(kind, content, [close]);
        close.addEventListener("click", function () {
            holder.removeChild(box);
        });
        holder.appendChild(box);
        return box;
    }

    function check(label, checked, hint) {
        var input = el("input", {type: "checkbox", checked: Boolean(checked)});
        var node = el("label", {className: "check"}, [
            input,
            el("span", null, [label, hint ? el("span", {className: "field-hint", text: hint})
                                          : null])
        ]);
        return {node: node, input: input};
    }

    function radio(name, label, checked, hint) {
        var input = el("input", {type: "radio", name: name, checked: Boolean(checked)});
        var node = el("label", {className: "check"}, [
            input,
            el("span", null, [label, hint ? el("span", {className: "field-hint"}, hint) : null])
        ]);
        return {node: node, input: input};
    }

    function resultLine(node, text, kind) {
        node.hidden = !text;
        node.className = "result-line" + (kind ? " " + kind : "");
        node.textContent = text || "";
    }

    /* ---------- Loading the state ---------------------------------------- */

    function loadState() {
        if (stateRequest) {
            return stateRequest;
        }
        stateRequest = SHELL.admin("/state?since=" + logSeq, undefined, {quiet: Boolean(state)})
            .then(function (data) {
                stateRequest = null;
                state = data;
                lastStateAt = Date.now();
                // The jobs first: the cards show which of their jobs run.
                applySnapshot(data.jobs, true);
                whenIdle("libraries", renderLibraries);
                whenIdle("orphans", renderOrphans);
                renderNotices();
                // Folder checks run in the background; ask again for their result.
                var checking = state.libraries.some(function (lib) {
                    return lib.enabled !== false && lib.status && lib.status.reachable === null;
                });
                if (checking && statusRetries < 15) {
                    statusRetries += 1;
                    window.setTimeout(loadState, 2000);
                } else if (!checking) {
                    statusRetries = 0;
                }
                return data;
            }, function (error) {
                stateRequest = null;
                if (!state) {
                    var status = byId("manage-status");
                    if (status) {
                        status.textContent = "The libraries could not be loaded: " + error.message +
                            " Trying again every " + (STATE_RETRY / 1000) + " seconds.";
                    }
                }
                throw error;
            });
        return stateRequest;
    }

    function refreshState() {
        loadState().catch(function () {
            // Shown already; the next poll tries again.
        });
    }

    function findLibrary(id) {
        var found = null;
        (state ? state.libraries : []).forEach(function (lib) {
            if (lib.id === id) {
                found = lib;
            }
        });
        return found;
    }

    function enabledLibraries() {
        return (state ? state.libraries : []).filter(function (lib) {
            return lib.enabled !== false;
        });
    }

    /* ---------- Notices -------------------------------------------------- */

    // How many videos the last scans found but could not read (no ffmpeg).
    function videosSkipped() {
        var count = 0;
        (state ? state.libraries : []).forEach(function (lib) {
            var job = lib.last_job;
            ((job && job.summary && job.summary.libraries) || []).forEach(function (item) {
                if (item.library_id === lib.id && typeof item.videos_skipped === "number") {
                    count += item.videos_skipped;
                }
            });
        });
        return count;
    }

    function renderNotices() {
        var holder = byId("manage-notices");
        var old = byId("no-endpoint-notice");
        if (old) {
            holder.removeChild(old);
        }
        var oldVideo = byId("no-ffmpeg-notice");
        if (oldVideo) {
            holder.removeChild(oldVideo);
        }
        var videos = state && state.videos;
        var skipped = videosSkipped();
        if (videos && !videos.available && skipped) {
            // Said once here, not in every scan's log line.
            var videoBox = notice("warn", fmt.plural(skipped, "video") + " " +
                (skipped === 1 ? "was" : "were") + " found but not indexed. " +
                (videos.problem || "Videos need ffmpeg and ffprobe.") +
                " Once that is fixed, scan again (see Videos in SETTINGS.md).");
            videoBox.id = "no-ffmpeg-notice";
            holder.appendChild(videoBox);
        }
        // An endpoint needs at least a model name before it can describe.
        var usable = state && state.endpoints.some(function (endpoint) {
            return endpoint.model && endpoint.enabled !== false;
        });
        if (state && !usable) {
            var box = notice("warn", "No AI model is set up yet, so pictures can be indexed " +
                "and found by file name, but not described. Add an endpoint (a model server " +
                "on this computer, or a cloud service) in Settings.", [
                el("a", {className: "button", href: "/settings", text: "Open Settings"})
            ]);
            box.id = "no-endpoint-notice";
            holder.insertBefore(box, holder.firstChild);
        }
        var oldMeaning = byId("meaning-notice");
        if (oldMeaning) {
            holder.removeChild(oldMeaning);
        }
        if (peopleBox && peopleBox.parentNode) {
            peopleBox.parentNode.removeChild(peopleBox);
        }
        peopleBox = peopleNotice(state && state.people);
        if (peopleBox) {
            holder.appendChild(peopleBox);
        }
        var meaning = state ? state.meaning : null;
        var described = state && state.libraries.some(function (lib) {
            return (lib.counts || {}).described > 0;
        });
        if (meaning && described && (!meaning.configured || !meaning.ready)) {
            var note = notice(meaning.configured ? "warn" : "info", meaning.configured
                ? "Search by meaning cannot be used: " + meaning.problem
                : "Search by meaning is off. It finds pictures by what a search means, not " +
                  "only by its words (\u201ckids playing in the snow\u201d finds a sledding " +
                  "picture), and makes \u201cMore like this\u201d smarter. To turn it on, " +
                  "set \"embedding\": {\"endpoint\": \"NAME\"} in config.json to an " +
                  "endpoint that serves an embedding model (see SETTINGS.md).");
            note.id = "meaning-notice";
            holder.appendChild(note);
        }
    }

    // People (faces, people.py): only shown when it is on, or face data is kept.
    var peopleBox = null;

    function peopleNotice(people) {
        if (!people || (!people.enabled && !people.stored)) {
            return null;
        }
        var kept = people.stored ? fmt.plural(people.faces, "face") + " found in " +
            fmt.plural(people.pictures, "picture") + " looked at" + (people.people
                ? ", " + fmt.plural(people.people, "person", "people") + " (" +
                  fmt.number(people.named || 0) + " named)" : "") + ", kept on this computer " +
            "(data/faces.db). Only the names ever leave it: to the model that writes trip " +
            "stories when that runs on this computer or your network, and in an export " +
            "with its People's names option." : "No faces found yet.";
        var text = !people.enabled ? "People is off; face data from before is kept: " + kept
            : people.ready ? "People: " + kept : "People cannot find faces: " + people.problem;
        var actions = [];
        if (people.stored || people.enabled) {
            actions.push(el("a", {className: "button small", href: "/people", text: "People page",
                                  title: "Name the people found, and put their faces right"}));
        }
        if (people.enabled) {
            actions.push(button("Check face helper", "small", function (event) {
                checkFaceHelper(event.currentTarget);
            }, {key: "people-check", title: "Start the face helper, load its models and " +
                "find a test face (a drawing)"}));
        }
        if (people.stored) {
            var busy = (snapshot ? [snapshot.current].concat(snapshot.queue || []) : [])
                .some(function (job) {
                    return job && job.kind === "delete_faces";
                });
            actions.push(button(busy ? "Delete face data (waiting)" : "Delete face data",
                "small danger-text", function () {
                    queueJob("delete_faces", null, {confirm: true});
                }, {key: "people-delete", disabled: busy, title: "Delete every face found " +
                    "(data/faces.db), of every library"}));
        }
        var box = notice(people.enabled && !people.ready ? "warn" : "info", text, actions);
        box.className += " people-notice";
        return box;
    }

    function checkFaceHelper(node) {
        if (node) {
            node.disabled = true;
        }
        return SHELL.admin("/people/check", {}).then(function (reply) {
            if (reply.ok) {
                SHELL.toast("The face helper works: Python " + reply.python + ", OpenCV " +
                            reply.opencv + ", numpy " + reply.numpy + " (" +
                            reply.seconds.toFixed(1) + " s).", "ok", 6);
            } else {
                SHELL.toast("The face helper does not work: " + reply.problem, "error", 10);
            }
        }, function () {
            // The message is shown already.
        }).then(function () {
            if (node) {
                node.disabled = false;
            }
        });
    }

    // Delete face data: every face of every library goes.
    function confirmDeleteFaces() {
        var people = (state && state.people) || {};
        var named = people.named || 0;
        return SHELL.confirm({
            title: "Delete all face data?",
            message: [
                el("p", {text: "The " + fmt.plural(people.faces || 0, "face") + " found in the " +
                    fmt.plural(people.pictures || 0, "picture") + " looked at " +
                    "(data/faces.db) are deleted, for every library, with the " +
                    fmt.plural(people.people || 0, "person", "people") + " they were grouped " +
                    "into" + (named ? " and the " + fmt.plural(named, "name") + " you gave " +
                              "them" : "") + ". This cannot be undone. The pictures " +
                    "themselves are not touched."}),
                el("p", {className: "muted", text: "Names an export published with its " +
                    "People's names option stay in that export, and person: searches saved " +
                    "as smart albums stay in those albums, until you delete them."}),
                el("p", {className: "muted", text: people.enabled ? "People is still on: " +
                    "the next Find faces (or scan) looks at every picture again. Turn it " +
                    "off in config.json (\"people\": {\"enabled\": false}) to keep it from " +
                    "doing so." : "People is off, so no faces are looked for again."})
            ],
            confirmText: "Delete face data",
            danger: true,
            // (Names are work that cannot be had back: typed out before they go.)
            typeName: named ? "delete" : undefined
        });
    }

    /* ---------- Library cards -------------------------------------------- */

    function renderLibraries() {
        var list = byId("library-list");
        var key = focusKey(list);
        clear(list);
        activityNodes = {};
        var libraries = state.libraries;
        byId("process-all").disabled = !enabledLibraries().length;
        if (!libraries.length) {
            list.appendChild(el("div", {className: "empty"}, [
                el("h2", {text: "Add your first library"}),
                el("p", {text: "A library is a folder of pictures on this computer or on a " +
                    "network share. Image Search looks through it, makes small previews, and " +
                    "asks an AI model to describe each picture, so you can search for what " +
                    "is in them. Your folders are only read, never changed."}),
                button("Add library", "primary", function () {
                    openDialog("add");
                }, {key: "empty-add"})
            ]));
        } else {
            libraries.forEach(function (lib) {
                list.appendChild(libraryCard(lib));
            });
        }
        updateActivity();
        restoreFocus(list, key);
    }

    function statusPill(lib) {
        if (lib.enabled === false) {
            return el("span", {className: "pill", text: "Turned off"});
        }
        var status = lib.status || {};
        if (status.reachable === true) {
            return el("span", {className: "pill ok", text: "Online",
                               title: "The folder can be reached."});
        }
        if (status.reachable === false) {
            return el("span", {className: "pill bad", text: "Offline",
                               title: status.message || "The folder cannot be reached."});
        }
        return el("span", {className: "pill", text: "Checking...",
                           title: "Checking whether the folder can be reached."});
    }

    function enabledSwitch(lib) {
        var input = el("input", {type: "checkbox", checked: lib.enabled !== false,
                                 attrs: {"aria-label": lib.name + " turned on"}});
        input.setAttribute("data-key", lib.id + ":enabled");
        var label = el("span", {text: lib.enabled !== false ? "On" : "Off"});
        input.addEventListener("change", function () {
            input.disabled = true;
            SHELL.admin("/libraries/" + encodeURIComponent(lib.id), {enabled: input.checked})
                .then(function () {
                    SHELL.toast(lib.name + (input.checked ? " is turned on." :
                        " is turned off: it is not searched and Process all skips it."), "ok");
                    refreshState();
                }, function () {
                    input.checked = !input.checked;
                    input.disabled = false;
                });
        });
        return el("label", {className: "switch",
                            title: "Turned-off libraries are not searched and are not scanned"},
                  [input, el("span", {className: "switch-track"}), label]);
    }

    function stat(label, value, title, kind) {
        return el("li", {className: "stat" + (kind ? " " + kind : ""), title: title}, [
            el("strong", {text: fmt.number(value)}), label
        ]);
    }

    function endpointText(lib) {
        var info = lib.endpoint_info || {};
        if (!info.ready) {
            if (info.problem) {
                return info.problem + " Its pictures are not described until that is fixed.";
            }
            if (lib.endpoint && !(info.endpoints || []).length) {
                return "This library names the endpoint " + lib.endpoint + ", which is " +
                    "missing or turned off, so its pictures cannot be described.";
            }
            return "No AI model is set up to describe this library's pictures yet.";
        }
        var text = "Described by " + (lib.endpoint ? info.name : "the default endpoint (" +
                                      info.name + ")");
        if (info.cloud) {
            text += ", which sends pictures to " + info.cloud_hosts.join(", ");
        }
        return text + ".";
    }

    function lastJobLine(lib) {
        var job = lib.last_job;
        if (!job) {
            // A library that is on already says so in its card note.
            return (lib.counts || {}).total || lib.enabled !== false ? null :
                el("div", {className: "last-job", text: "Not scanned yet."});
        }
        if (!job.finished) {
            // Running or waiting: the activity box above shows it.
            return null;
        }
        var when = fmt.relative(job.finished);
        var parts = ["Last job: " + J.kind(job), J.state(job.state)];
        if (when) {
            parts.push(when);
        }
        var summary = J.summary(job);
        if (summary) {
            parts.push(summary);
        }
        return el("div", {className: "last-job"}, [
            parts.join(" \u00b7 "),
            job.error && job.state !== "interrupted" ?
                el("div", {className: "error-text", text: job.error}) : null
        ]);
    }

    function libraryCard(lib) {
        var counts = lib.counts || {};
        var info = lib.endpoint_info || {};
        var off = lib.enabled === false;
        var offline = !off && lib.status && lib.status.reachable === false;
        var card = el("article", {className: "card" + (off ? " is-off" : ""),
                                  id: "library-" + lib.id});
        var root = lib.root || lib.path || "";

        card.appendChild(el("div", {className: "card-head"}, [
            el("h2", {text: lib.name}),
            statusPill(lib),
            info.cloud ? el("span", {className: "pill warn plain",
                                     text: "Sends pictures to " + info.cloud_hosts.join(", "),
                                     title: "This library is described by a cloud service."})
                : null,
            enabledSwitch(lib)
        ]));
        card.appendChild(el("div", {className: "path-line"}, [
            el("span", {className: "path-text", text: root, title: root}),
            button("Copy", "subtle small", function () {
                SHELL.copyText(root).then(function (ok) {
                    SHELL.toast(ok ? "Folder path copied." : "The path could not be copied.",
                                ok ? "ok" : "error", 3);
                });
            }, {key: lib.id + ":copy", title: "Copy the folder path"})
        ]));

        if (off) {
            card.appendChild(el("p", {className: "card-note", text: "This library is turned " +
                "off: it is not searched, and Process all skips it. Turn it on to scan or " +
                "describe it."}));
        } else if (offline) {
            card.appendChild(notice("warn", (lib.status.message || "The folder cannot be " +
                "reached.") + " Scans skip it and nothing is removed until it is back."));
        }

        if (counts.present) {
            var pct = fmt.percent(counts.described, counts.present);
            card.appendChild(el("div", {className: "described"}, [
                el("div", {className: "described-line"}, [
                    el("span", null, [el("strong", {text: fmt.number(counts.described)}),
                                      " of " + fmt.plural(counts.present, "picture") +
                                      " described"]),
                    el("span", {text: pct + "%"})
                ]),
                progressBar(pct, {doneColor: true, label: lib.name + ": pictures described"})
            ]));
            var stats = el("ul", {className: "stats"});
            stats.appendChild(stat("to describe", counts.pending,
                                   "Pictures that have no description yet"));
            if (counts.stale) {
                stats.appendChild(stat("out of date", counts.stale, "Described with an " +
                    "older version of the instructions, or videos whose descriptions talk " +
                    "about their frames; Re-describe updates them", "warn"));
            }
            if (counts.failed) {
                stats.appendChild(stat("failed", counts.failed, "Pictures the AI model could " +
                    "not describe" + (counts.gave_up ? " (" + fmt.number(counts.gave_up) +
                    " gave up after 3 tries; Retry failed tries them again)" : ""), "bad"));
            }
            if (counts.missing) {
                stats.appendChild(stat("missing", counts.missing, "Files that are no longer " +
                    "in the folder; they are forgotten after 30 days", "warn"));
            }
            if (counts.hidden) {
                stats.appendChild(stat("hidden", counts.hidden, "Pictures hidden from search"));
            }
            var videoInfo = lib.video_info || {};
            if (videoInfo.count) {
                stats.appendChild(stat(videoInfo.count === 1 ? "video" : "videos",
                                       videoInfo.count, "Videos among the pictures: each is " +
                                       "shown and described from a few of its frames"));
            }
            if (counts.pending_hidden) {
                stats.appendChild(stat("hidden, kept here", counts.pending_hidden, "Hidden " +
                    "pictures are never sent to " + (info.cloud_hosts || []).join(", ") +
                    ", so they stay undescribed unless an endpoint on this computer or " +
                    "network describes them"));
            }
            card.appendChild(stats);
        } else if (!off) {
            card.appendChild(el("p", {className: "card-note", text: counts.missing ?
                "No pictures are in the folder right now." :
                "No pictures are indexed yet. Scan + describe finds them and describes them."}));
        }

        var activity = el("div", {className: "card-activity", hidden: true});
        activityNodes[lib.id] = activity;
        card.appendChild(activity);
        SHELL.append(card, lastJobLine(lib));
        SHELL.append(card, lastExportLine(lib));
        var endpointNote = el("p", {className: "card-note" + (info.ready ? "" : " card-problem"),
                                    text: endpointText(lib)},
                              info.ready ? null : [" ", el("a", {href: "/settings",
                                                                 text: "Open Settings."})]);
        card.appendChild(endpointNote);
        if (info.ready && info.warning) {
            card.appendChild(el("p", {className: "card-note card-problem", text: info.warning +
                " Start it before describing; scanning works without it."}));
        }
        var proxyInfo = (lib.video_info || {}).proxies || {};
        var proxiesOn = state.videos && state.videos.proxies && state.videos.proxies.enabled;
        if (proxiesOn && (lib.video_info || {}).count) {
            card.appendChild(el("p", {className: "card-note", text: "Playable copies of " +
                "videos (H.264 copies of the videos browsers may not play, in the data " +
                "folder): " + (proxyInfo.count ? fmt.plural(proxyInfo.count, "video") + ", " +
                R.bytes(proxyInfo.bytes || 0) : "none yet") + (proxyInfo.pending ? "; " +
                fmt.plural(proxyInfo.pending, "video") + " still " +
                (proxyInfo.pending === 1 ? "needs" : "need") + " one" : "") + "."}));
        }
        var driveInfo = (lib.video_info || {}).drives || null;
        var drivePreviews = state.videos && state.videos.drive_previews || {};
        if (driveInfo && driveInfo.count) {
            // Runs of clips recorded one after the other (drives.py).
            var previewInfo = driveInfo.previews || {};
            card.appendChild(el("p", {className: "card-note", text: "Drives (video clips " +
                "recorded one after the other): " + fmt.plural(driveInfo.count, "drive") + ", " +
                fmt.duration(driveInfo.seconds) + " of video, " +
                fmt.number(driveInfo.described) + " described" +
                (drivePreviews.enabled ? "; sped-up previews: " + (previewInfo.count
                    ? fmt.number(previewInfo.count) + ", " + R.bytes(previewInfo.bytes || 0)
                    : "none yet") + (previewInfo.pending ? " (" +
                    fmt.number(previewInfo.pending) + " to make)" : "") : "") + "."}));
        }
        var meaningInfo = lib.meaning;
        var meaningSetup = (state && state.meaning) || {};
        if (meaningInfo && meaningSetup.configured && meaningInfo.pictures) {
            card.appendChild(el("p", {className: "card-note meaning-line"}, [
                "Search by meaning: ",
                el("strong", {text: fmt.number(meaningInfo.embedded)}),
                " of " + fmt.plural(meaningInfo.pictures, "picture") +
                    (meaningInfo.pending ? " (" + fmt.number(meaningInfo.pending) +
                     " to update)" : "") + "." +
                    (meaningInfo.held_back ? " " + fmt.plural(meaningInfo.held_back,
                                                              "hidden picture") +
                     " " + (meaningInfo.held_back === 1 ? "is" : "are") + " never sent to " +
                     "the cloud embedding endpoint." : "")
            ]));
        }
        var looksInfo = lib.looks;
        var looksSetup = (state && state.looks) || {};
        if (looksInfo && looksInfo.pictures && (looksSetup.configured || looksInfo.embedded)) {
            // (mismatch: the last update stopped at the probe check, see looks.py.)
            card.appendChild(el("p", {className: "card-note looks-line" +
                                      (looksSetup.mismatch ? " card-problem" : ""),
                                      title: looksSetup.mismatch || ""}, [
                "Looks: ",
                el("strong", {text: fmt.number(looksInfo.embedded)}),
                " of " + fmt.plural(looksInfo.pictures, "picture") +
                    (looksSetup.changed ? " (made with other settings: rebuild them to search " +
                        "by an example picture)" : looksSetup.mismatch ? " (the last update " +
                        "stopped: the image model makes other vectors than the reference; see " +
                        "the job's log)" : looksInfo.pending ? " (" +
                        fmt.number(looksInfo.pending) + " to make: a GPU job)" : "") + "."
            ]));
        }
        var facesInfo = lib.faces;
        var peopleSetup = (state && state.people) || {};
        if (facesInfo && facesInfo.pictures && (peopleSetup.enabled || facesInfo.done)) {
            // "Faces: 7 in 4 pictures (4 of 7 pictures looked at). 3 to look at, ..."
            // (Wide panoramas and very large pictures are looked at in full size,
            // videos in several frames; the clips of dashcams' drives are skipped:
            // SHELL.facesNote.)
            card.appendChild(el("p", {className: "card-note faces-line"}, [
                "Faces: ",
                el("strong", {text: fmt.number(facesInfo.faces)}),
                SHELL.facesNote(facesInfo, Boolean(peopleSetup.enabled))
            ]));
        }
        var refining = lib.refine_info || {};
        if (refining.enabled) {
            card.appendChild(el("p", {className: "card-note" + (refining.ready ? "" :
                " card-problem"), text: refining.ready ? "Documents and screenshots are " +
                "described again (refined) by " + refining.settings.endpoint +
                (refining.cloud ? ", which sends them to " + refining.cloud_hosts.join(", ")
                    : "") + "." + (refining.hidden ? " " + fmt.plural(refining.hidden,
                    "hidden picture") + " stay as they are (hidden pictures are not sent " +
                    "there)." : "") : refining.problem}));
        }

        var actions = el("div", {className: "card-actions"});
        var noModel = !info.ready;
        var modelTitle = noModel ? (info.problem || "Set up an AI model in Settings first")
            : undefined;

        // A job button: disabled (and saying so) while the same job is
        // running or waiting, since starting it again would only repeat it.
        function jobButton(text, className, kind, params, key, disabled, title) {
            var busy = busyJob(lib, kind, params);
            var state = busy ? {queued: "waiting", paused: "paused"}[busy.state] || "running"
                : "";
            return button(busy ? text + " (" + state + ")" : text, className, function () {
                queueJob(kind, lib, params);
            }, {key: key, disabled: disabled || Boolean(busy),
                title: busy ? "This job is " + (state === "waiting" ? "waiting to run" : state) +
                    "; see the job panel" : title});
        }

        actions.appendChild(jobButton("Scan + describe", "primary", "process", {},
            lib.id + ":process", off || noModel,
            modelTitle || "Look for new and changed pictures, then describe the new ones"));
        actions.appendChild(jobButton("Scan", "", "scan", {}, lib.id + ":scan", off,
            "Look for new, changed and removed pictures, without describing them"));
        actions.appendChild(jobButton("Describe", "", "describe", {}, lib.id + ":describe",
            off || noModel || !counts.pending,
            modelTitle || (counts.pending ? "Describe the " +
                fmt.plural(counts.pending, "picture") + " that have no description yet"
                : "Every picture is described")));
        if (counts.stale) {
            actions.appendChild(jobButton("Re-describe out of date (" +
                fmt.number(counts.stale) + ")", "", "describe", {redescribe_stale: true},
                lib.id + ":stale", off || noModel,
                modelTitle || "Describe again the pictures described with older instructions, " +
                "and videos whose descriptions talk about their frames"));
        }
        var refineInfo = lib.refine_info || {};
        if (refineInfo.enabled) {
            var refinePending = refineInfo.pending;
            actions.appendChild(jobButton("Refine documents" + (typeof refinePending === "number" ?
                " (" + fmt.number(refinePending) + ")" : ""), "", "refine", {}, lib.id + ":refine",
                off || !refineInfo.ready || refinePending === 0,
                !refineInfo.ready ? refineInfo.problem : refinePending === 0 ?
                    "Every document and screenshot is refined already" :
                    "Describe the documents and screenshots again with " +
                    refineInfo.settings.endpoint + ", which reads their text more fully"));
            if (refineInfo.failed) {
                actions.appendChild(jobButton("Retry failed refine (" +
                    fmt.number(refineInfo.failed) + ")", "", "refine", {retry_failed: true},
                    lib.id + ":refine-retry", off || !refineInfo.ready,
                    !refineInfo.ready ? refineInfo.problem : "Try again to refine the " +
                        fmt.plural(refineInfo.failed, "picture") + " that failed 3 times, " +
                        "with the others still waiting (their earlier descriptions are kept " +
                        "until then)"));
            }
        }
        if (counts.failed) {
            actions.appendChild(jobButton("Retry failed (" + fmt.number(counts.failed) + ")",
                "", "describe", {retry_failed: true}, lib.id + ":retry", off || noModel,
                modelTitle || "Try again to describe the pictures that failed"));
        }
        if (meaningInfo && meaningSetup.configured && meaningInfo.pending) {
            actions.appendChild(jobButton("Update meaning search (" +
                fmt.number(meaningInfo.pending) + ")", "", "embed", {}, lib.id + ":embed",
                off || !meaningSetup.ready, !meaningSetup.ready ? meaningSetup.problem :
                    "Make the meaning vectors of the " + fmt.plural(meaningInfo.pending,
                    "picture") + " whose description is new or changed, so search by " +
                    "meaning and More like this find them (" + meaningSetup.endpoint + ")"));
        }
        if (looksInfo && looksSetup.configured &&
                (looksInfo.pending || looksInfo.views || looksSetup.mismatch)) {
            var rebuild = Boolean(looksSetup.changed || looksSetup.mismatch);
            // (Only opening views of 360 photos to work out: said so.)
            var looksCount = rebuild ? fmt.number(looksInfo.pictures) : looksInfo.pending
                ? fmt.number(looksInfo.pending) : fmt.plural(looksInfo.views, "opening view");
            actions.appendChild(jobButton((rebuild ? "Rebuild looks (" : "Update looks (") +
                looksCount + ")", "",
                "image_embed", rebuild ? {rebuild: true} : {}, lib.id + ":looks",
                off || !looksSetup.ready, !looksSetup.ready ? looksSetup.problem :
                    looksSetup.mismatch ? "The image model makes other vectors of the probe " +
                    "pictures than the reference. If its server is set up right (flash " +
                    "attention off), throw away the reference and the kept looks and make " +
                    "them again" : rebuild ?
                    "The looks were made with other settings (another model, dims or picture " +
                    "size): throw them all away and make them again" : !looksInfo.pending ?
                    "Work out where " + fmt.plural(looksInfo.views, "described 360 photo") +
                    " open (their words against views of them, " + looksSetup.endpoint + ")" :
                    "Make the image vectors of the " + fmt.plural(looksInfo.pending, "picture") +
                    " that have none, so Looks like this and search by an example picture " +
                    "find them (" + looksSetup.endpoint + "; best on a GPU)" +
                    (looksInfo.views ? "; and work out where " +
                     fmt.plural(looksInfo.views, "described 360 photo") + " open" : "")));
        }
        if (facesInfo && peopleSetup.enabled && (facesInfo.pending || facesInfo.failed)) {
            var retryFaces = !facesInfo.pending;
            actions.appendChild(jobButton((retryFaces ? "Retry finding faces (" :
                "Find faces (") + fmt.number(retryFaces ? facesInfo.failed : facesInfo.pending) +
                ")", "", "faces", retryFaces ? {retry_failed: true} : {}, lib.id + ":faces",
                off || !peopleSetup.ready, !peopleSetup.ready ? peopleSetup.problem :
                    retryFaces ? "Look again at the " + fmt.plural(facesInfo.failed, "picture") +
                    " that could not be looked at 3 times" :
                    "Find the faces in the " + fmt.plural(facesInfo.pending, "picture") +
                    " not looked at yet, on this computer's processor (nothing is sent " +
                    "anywhere)"));
        }
        if (lib.previews !== false) {
            actions.appendChild(jobButton("Build previews", "", "previews", {},
                lib.id + ":previews", off,
                "Make the screen-sized previews that are still missing"));
        }
        if (proxiesOn && proxyInfo.pending) {
            actions.appendChild(jobButton("Make playable copies (" +
                fmt.number(proxyInfo.pending) + ")", "", "proxies", {}, lib.id + ":proxies", off,
                "Make H.264 copies of the videos most browsers cannot play (HEVC and " +
                "others), so they play in the viewer; this takes a while"));
        }
        if (drivePreviews.enabled && driveInfo && (driveInfo.previews || {}).pending) {
            actions.appendChild(jobButton("Make sped-up drives (" +
                fmt.number(driveInfo.previews.pending) + ")", "", "drive_previews", {},
                lib.id + ":drive_previews", off,
                "Make a sped-up copy of each drive (" + drivePreviews.speed + " times faster, " +
                "H.264), which the viewer can play instead of the clips; this takes a while"));
        }
        var exporting = exportJobOf(lib);
        actions.appendChild(button(exporting ? "Export... (" + (exporting.state === "queued" ?
            "waiting" : "running") + ")" : "Export...", "", function () {
            openDialog("export", lib.id);
        }, {key: lib.id + ":export", disabled: off,
            title: off ? "Turn the library on to export it" : "Make a copy of this library " +
                "that works as a website on its own: the same search, on any web host"}));
        actions.appendChild(el("span", {className: "spacer"}));
        if (counts.failed) {
            actions.appendChild(button("Failures", "subtle", function () {
                openDialog("failures", lib.id);
            }, {key: lib.id + ":failures", title: "List the pictures that could not be described"}));
        }
        actions.appendChild(button("Edit", "subtle", function () {
            openDialog("edit", lib.id);
        }, {key: lib.id + ":edit", title: "Change the folder, name and other settings"}));
        actions.appendChild(button("Remove", "subtle danger-text", function () {
            openDialog("remove", lib.id);
        }, {key: lib.id + ":remove", title: "Remove this library"}));
        card.appendChild(actions);
        return card;
    }

    // "Exported 2 days ago: 312 pictures, 162 MB" with a button to open the folder.
    function lastExportLine(lib) {
        var last = lib.export_info && lib.export_info.last;
        if (!last) {
            return null;
        }
        var text = "Last export: " + fmt.relative(last.updated) + ", " +
            fmt.plural(last.pictures, "picture") + ", " + fmt.bytes(last.bytes) +
            (last.finished ? "" : " (stopped before it finished)");
        return el("div", {className: "last-job export-line"}, [
            el("span", {text: text}),
            button("Open folder", "subtle small", function () {
                openExportFolder(lib);
            }, {key: lib.id + ":export-folder", title: "Show " + last.output +
                " in the file manager"}),
            last.finished ? button("How to publish", "subtle small", function () {
                openDialog("export", lib.id, "done");
            }, {key: lib.id + ":export-done", title: "Where the site is and how to put it " +
                "on the web"}) : null
        ]);
    }

    function openExportFolder(lib) {
        return SHELL.admin(lib.trip ? "/trips/" + encodeURIComponent(lib.trip) +
                           "/export/open-folder" : "/exports/" + encodeURIComponent(lib.id) +
                           "/open-folder", {})
            .then(function (reply) {
                SHELL.toast("Opened " + reply.path + " in the file manager.", "ok", 4);
            }, function () {
                // The message is shown already.
            });
    }

    // The "running now" / "waiting to run" box of each card, from the jobs.
    function updateActivity() {
        var current = snapshot && snapshot.current;
        var queue = snapshot ? snapshot.queue : [];
        Object.keys(activityNodes).forEach(function (id) {
            var box = activityNodes[id];
            clear(box);
            var waiting = queue.filter(function (job) {
                return job.library_id === id;
            });
            if (current && current.library_id === id) {
                var value = current.total ? fmt.percent(current.done, current.total) : null;
                box.appendChild(el("div", null, [
                    el("strong", {text: J.state(current.state) + ": " + J.kind(current)}),
                    " \u00b7 " + J.stage(current.stage, current),
                    current.total ? " \u00b7 " + J.progress(current) : ""
                ]));
                box.appendChild(progressBar(value, {still: current.state !== "running",
                                                    label: libraryName(id) + ": job progress"}));
            }
            if (waiting.length) {
                box.appendChild(el("div", {text: "Waiting to run: " + waiting.map(function (job) {
                    return J.kind(job);
                }).join(", ")}));
            }
            box.hidden = !box.firstChild;
        });
    }

    function libraryName(id) {
        var lib = findLibrary(id);
        return lib ? lib.name : id;
    }

    /* ---------- Queueing jobs -------------------------------------------- */

    function sameParams(first, second) {
        var a = first || {};
        var b = second || {};
        var keys = Object.keys(a).concat(Object.keys(b));
        return keys.every(function (key) {
            return Boolean(a[key]) === Boolean(b[key]) && (a[key] || null) === (b[key] || null);
        });
    }

    // The running or waiting job that does the same as kind + params for lib.
    function busyJob(lib, kind, params) {
        var jobs = snapshot ? (snapshot.current ? [snapshot.current] : []).concat(snapshot.queue)
            : [];
        var found = null;
        jobs.forEach(function (job) {
            if (!found && job.kind === kind && job.library_id === lib.id &&
                    job.state !== "cancelling" && sameParams(job.params, params)) {
                found = job;
            }
        });
        return found;
    }

    // True for an export job of a library, or of a trip (lib.trip: its params' trip).
    function exportsLib(job, lib) {
        return job.kind === "export" && (lib.trip ? (job.params || {}).trip === lib.trip
            : job.library_id === lib.id);
    }

    // The export job of a library that is running or waiting, or null.
    function exportJobOf(lib) {
        var jobs = snapshot ? (snapshot.current ? [snapshot.current] : []).concat(snapshot.queue)
            : [];
        var found = null;
        jobs.forEach(function (job) {
            if (!found && exportsLib(job, lib) && job.state !== "cancelling") {
                found = job;
            }
        });
        return found;
    }

    // The last finished export job of a library (in the recent jobs), or null.
    function lastExportJob(lib) {
        var found = null;
        (snapshot ? snapshot.recent : []).forEach(function (job) {
            if (!found && exportsLib(job, lib)) {
                found = job;
            }
        });
        return found;
    }

    // The job with this id in a snapshot (current, waiting or finished), or null.
    function jobById(data, id) {
        var all = (data.current ? [data.current] : []).concat(data.queue || [],
                                                               data.recent || []);
        for (var k = 0; k < all.length; k++) {
            if (all[k].id === id) {
                return all[k];
            }
        }
        return null;
    }

    function cloudKey(lib) {
        return CLOUD_OK_KEY + lib.id;
    }

    function cloudConfirmed(lib) {
        return SHELL.storageGet(cloudKey(lib)) ===
            (lib.endpoint_info.cloud_hosts || []).join(",");
    }

    function testCost(lib) {
        var names = (lib.endpoint_info && lib.endpoint_info.endpoints) || [];
        var text = names.length ? SHELL.storageGet(TEST_COST_KEY + names[0]) : null;
        var value = text === null ? NaN : Number(text);
        return isFinite(value) && value >= 0 ? value : null;
    }

    // How many pictures a job sends to be described: a number, or null when
    // only the scan can tell (a library that has not been scanned yet).
    function jobImages(lib, kind, params) {
        var counts = lib.counts || {};
        if (params.retry_failed) {
            return counts.failed || 0;
        }
        if (params.redescribe_stale) {
            return (counts.stale || 0) + (counts.pending || 0);
        }
        if (kind === "process" && !counts.present) {
            return null;
        }
        return counts.pending || 0;
    }

    // One line of the cloud question: what goes where, and what it costs.
    function cloudLine(lib, kind, params, found) {
        var count = jobImages(lib, kind, params);
        var cost = testCost(lib);
        var hosts = lib.endpoint_info.cloud_hosts.join(", ");
        var text;
        if (count === null) {
            text = lib.name + ": every picture the scan finds (" + (found ?
                (found.more ? "at least " : "about ") + fmt.plural(found.count, "picture") +
                " by the folder check" : "the number is known after the scan") + "), to " +
                hosts;
            if (found && cost !== null && found.count) {
                text += ", about " + money(cost * found.count) + " or more";
            }
        } else {
            text = lib.name + ": " + (kind === "process" ? "about " : "") +
                fmt.plural(count, "picture") + (kind === "process" ?
                    " not described yet, plus any new ones the scan finds," : "") + " to " + hosts;
            if (cost !== null && count) {
                text += ", about " + money(cost * count);
            }
        }
        if (cost !== null) {
            text += " (" + money(cost) + " per picture, measured by the last Test)";
        }
        return {text: text, costKnown: cost !== null};
    }

    // The first time a library that uses a cloud service is described, say
    // how many pictures go where (and the cost, when a Test has measured it).
    // found: {count, more} from the folder check of a library just added.
    function confirmCloud(kind, libraries, params, found) {
        if (kind !== "describe" && kind !== "process") {
            return Promise.resolve(true);
        }
        var risky = libraries.filter(function (lib) {
            return lib.enabled !== false && lib.endpoint_info && lib.endpoint_info.cloud &&
                !cloudConfirmed(lib);
        });
        if (!risky.length) {
            return Promise.resolve(true);
        }
        var unknownCost = false;
        // A video whose first description talks about its frames is asked
        // once more (describe.describe_checked): up to two requests.
        var sheetCheck = !(state && state.videos && state.videos.sheet_check === false);
        var withVideos = sheetCheck && risky.some(function (lib) {
            return Boolean((lib.video_info || {}).count);
        });
        var items = risky.map(function (lib) {
            var line = cloudLine(lib, kind, params, found);
            if (!line.costKnown) {
                unknownCost = true;
            }
            return el("li", {text: line.text});
        });
        var message = [
            el("p", {text: "These libraries are described by a service outside this computer " +
                "and your network. Each picture is resized and sent there with the " +
                "describing instructions and the library's context notes. File names, " +
                "folders and photo details such as the location stay here."}),
            el("ul", {className: "warning-list"}, items),
            unknownCost ? el("p", {text: "The service charges for each picture. Test the " +
                "endpoint on the Settings page to see what one picture costs."}) : null,
            withVideos ? el("p", {text: "A video whose first description talks about its " +
                "frames instead of the video is asked once more, so a video can cost up to " +
                "twice as much as a picture."}) : null,
            el("p", {className: "muted", text: "You are asked once for each library."})
        ];
        return SHELL.confirm({
            title: "Send pictures to a cloud service?",
            message: message,
            confirmText: "Send pictures"
        }).then(function (ok) {
            if (ok) {
                risky.forEach(function (lib) {
                    SHELL.storageSet(cloudKey(lib), (lib.endpoint_info.cloud_hosts || []).join(","));
                });
            }
            return ok;
        });
    }

    function jobName(kind, lib, params) {
        return J.title({kind: kind, library_id: lib ? lib.id : null,
                        library: lib ? lib.name : "", params: params});
    }

    // A refine job whose endpoint is a cloud service: say what goes where (every time).
    function confirmRefine(lib, params) {
        var info = lib && lib.refine_info;
        if (!info || !info.cloud) {
            return Promise.resolve(true);
        }
        // (The counts leave out hidden pictures, which are never sent there.)
        var number = params && params.retry_failed && typeof info.pending === "number" ?
            info.pending + (info.failed || 0) : info.pending;
        var count = typeof number === "number" ? fmt.plural(number, "picture") :
            "The documents and screenshots";
        return SHELL.confirm({
            title: "Send documents to a cloud service?",
            message: [
                el("p", {text: count + " of " + lib.name + " will be sent to " +
                    info.cloud_hosts.join(", ") + " (the refine endpoint " +
                    info.settings.endpoint + ") to be described again. Documents and " +
                    "screenshots often show private text; hidden pictures are never sent."}),
                el("p", {className: "muted", text: "The service charges for each picture."})
            ],
            confirmText: "Send pictures"
        });
    }

    // Rebuild looks: every kept image vector goes (of every library), and making
    // them again is a long run of the image model.
    function confirmRebuildLooks() {
        var setup = (state && state.looks) || {};
        var kept = fmt.plural(setup.stored || 0, "image vector");
        return SHELL.confirm({
            title: "Rebuild the looks?",
            message: [
                el("p", {text: setup.mismatch && !setup.changed
                    ? "The image model now makes other vectors of three test pictures than " +
                      "the reference ones the looks are checked against. That happens when " +
                      "its server runs with flash attention on (or in float16): if so, fix " +
                      "the server and choose Update looks instead. If the server is right, " +
                      "the reference was not: rebuilding throws it away with the " + kept +
                      " kept, for every library, and makes them again."
                    : "The " + kept + " kept were made with other settings (another image " +
                      "model or projector, dims or picture size). Rebuilding throws them all " +
                      "away, for every library, and makes them again with the current " +
                      "settings."}),
                el("p", {className: "muted", text: "This is a long job for the image model: " +
                    "hours on a CPU, much less on a GPU. Until it is done, Looks like this " +
                    "finds only the pictures made again." + (setup.reference ? "" :
                    " Its first pictures make the new reference: run the first few on a CPU " +
                    "server (or with flash attention off) so it can be trusted, for instance " +
                    "with scan.py --embed-images --rebuild-looks --limit 8.")})
            ],
            confirmText: "Rebuild looks"
        });
    }

    function queueJob(kind, lib, params, found) {
        params = params || {};
        var libraries = lib ? [lib] : enabledLibraries();
        var ask = kind === "refine" ? confirmRefine(lib, params) :
            kind === "image_embed" && params.rebuild ? confirmRebuildLooks() :
            kind === "delete_faces" ? confirmDeleteFaces() :
            confirmCloud(kind, libraries, params, found);
        return ask.then(function (ok) {
            if (!ok) {
                return null;
            }
            return SHELL.admin("/jobs", {kind: kind, library_id: lib ? lib.id : null,
                                         params: params}).then(function (reply) {
                var state = reply.job ? reply.job.state : "";
                SHELL.toast(reply.added ? jobName(kind, lib, params) + " will start shortly." :
                            state === "running" || state === "paused" ?
                                "That job is running already." :
                                "That job is already waiting to run.",
                            reply.added ? "ok" : "info", 4);
                applySnapshot(reply.jobs);
                return reply;
            });
        }).catch(function () {
            // The message is shown already.
        });
    }

    /* ---------- Jobs ----------------------------------------------------- */

    function pollJobs() {
        window.clearTimeout(pollTimer);
        SHELL.admin("/jobs?since=" + logSeq, undefined, {quiet: true}).then(function (data) {
            applySnapshot(data);
        }, function () {
            showServerGone();
            pollTimer = window.setTimeout(pollJobs, POLL_IDLE);
        });
    }

    function showServerGone() {
        if (byId("server-gone")) {
            return;
        }
        var box = notice("error", "The server is not answering. Is it still running? This " +
                         "page tries again every 10 seconds.");
        box.id = "server-gone";
        byId("manage-notices").appendChild(box);
    }

    function active() {
        return Boolean(snapshot && (snapshot.current || snapshot.queue.length ||
                                    (snapshot.lock && snapshot.lock.busy)));
    }

    function applySnapshot(data, fromState) {
        if (!data) {
            return;
        }
        var gone = byId("server-gone");
        if (gone) {
            gone.parentNode.removeChild(gone);
        }
        if (data.seq < logSeq) {
            // The server was restarted: its log starts again.
            logLines = [];
            logSeq = 0;
            byId("job-log").textContent = "";
        }
        var fresh = data.log.filter(function (line) {
            return line.seq > logSeq;
        });
        logSeq = Math.max(logSeq, data.seq);
        snapshot = data;
        snapshotListeners.slice().forEach(function (listener) {
            listener(data);
        });
        SHELL.showJobs(data);
        addLogLines(fresh);
        whenIdle("jobs", renderJobs);
        updateActivity();

        var key = [data.current ? data.current.id + ":" + data.current.state : "-",
                   data.recent.length ? data.recent[0].id + ":" + data.recent[0].state : "-",
                   data.queue.map(function (job) {
                       return job.id;
                   }).join(",")].join("|");
        if (jobKey !== null && key !== jobKey && !fromState) {
            refreshState();
        } else if (data.current && !fromState && Date.now() - lastStateAt > STATE_REFRESH) {
            refreshState();
        }
        jobKey = key;
        window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(pollJobs, active() ? POLL_ACTIVE : POLL_IDLE);
    }

    function renderJobs() {
        renderCurrent();
        renderQueue();
        renderRecent();
    }

    function currentKey(data) {
        var job = data.current;
        return [job ? job.id : "-", job ? job.state : "-", job && job.waiting ? "w" : "",
                job && job.remote ? "x" : "",
                data.lock && data.lock.other ? "o" + (data.lock.kind || "") : "",
                data.manager.running ? "r" : "", data.manager.message || ""].join("|");
    }

    function askOtherToStop() {
        SHELL.admin("/abort-other", {}).then(function (reply) {
            SHELL.toast(reply.message, reply.requested ? "ok" : "info");
            pollJobs();
        });
    }

    function jobAction(job, action) {
        return SHELL.admin("/jobs/" + job.id + "/" + action, {}).then(function (reply) {
            applySnapshot(reply.jobs);
        }, function () {
            pollJobs();
        });
    }

    function cancelJob(job) {
        SHELL.confirm({
            title: "Stop this job?",
            message: "\u201c" + J.title(job) + "\u201d stops after the picture it is working " +
                "on. Everything done so far is kept; running the job again carries on " +
                "where it stopped.",
            confirmText: "Stop the job",
            cancelText: "Keep it running"
        }).then(function (yes) {
            if (yes) {
                jobAction(job, "cancel");
            }
        });
    }

    function renderCurrent() {
        var holder = byId("job-current");
        var data = snapshot;
        var key = currentKey(data);
        if (currentView && currentView.key === key) {
            currentView.update(data.current);
            return;
        }
        var focus = focusKey(holder);
        clear(holder);
        currentView = {key: key, update: function () {}};
        if (!data.manager.running) {
            holder.appendChild(notice("error", data.manager.message ||
                "Jobs are not running in this server process, so queued jobs wait."));
        }
        var cli = data.lock && data.lock.other && data.lock.kind !== "server";
        if (data.lock && data.lock.other) {
            holder.appendChild(notice("warn", J.otherHolder(data.lock) + " (" +
                data.lock.holder + "). Jobs started here wait until it finishes." +
                (cli ? "" : " Pause or cancel it on that server's Manage page."),
                [cli ? button("Ask it to stop", "small", askOtherToStop, {key: "abort-other"})
                    : null]));
        }
        var job = data.current;
        if (!job) {
            var waiting = data.lock && data.lock.other;
            holder.appendChild(el("p", {className: "muted", text: waiting ?
                (data.queue.length ? "Queued jobs start when it finishes."
                    : "No job started from this page is running.") :
                data.queue.length ? "Starting the next job..." : "Nothing is running. " +
                "Use the buttons on a library, or Process all libraries, to scan and " +
                "describe pictures."}));
            restoreFocus(holder, focus);
            return;
        }
        var title = el("div", {className: "job-title"});
        var stage = el("div", {className: "job-meta"});
        var bar = progressBar(null, {label: "Current job progress"});
        var numbers = el("div", {className: "job-meta"});
        var item = el("div", {className: "job-item"});
        var problems = el("div", {className: "job-meta"});
        holder.appendChild(el("div", {className: "card-head"}, [title,
            el("span", {className: "pill " + (job.state === "running" ? "info" :
                                              job.state === "paused" ? "warn" : ""),
                        text: job.waiting ? "Waiting" : J.state(job.state)})]));
        holder.appendChild(stage);
        holder.appendChild(bar);
        holder.appendChild(numbers);
        holder.appendChild(item);
        holder.appendChild(problems);
        if (job.remote) {
            holder.appendChild(el("p", {className: "muted", text: "Run by another server that " +
                "uses the same data folder (" + (job.holder || "another process") + "). Its " +
                "progress here is updated every few seconds; pause or cancel it on that " +
                "server's Manage page."}));
        }
        var actions = el("div", {className: "actions"});
        if (job.remote) {
            // Read-only: the other server owns this job.
            actions.hidden = true;
        } else if (job.state === "running" && !job.waiting) {
            actions.appendChild(button("Pause", "", function () {
                jobAction(job, "pause");
            }, {key: "job-pause", title: "Pause after the current picture; Resume carries on"}));
        }
        if (job.state === "paused") {
            actions.appendChild(button("Resume", "primary", function () {
                jobAction(job, "resume");
            }, {key: "job-resume"}));
        }
        if (job.state !== "cancelling" && !job.remote) {
            actions.appendChild(button("Cancel", "", function () {
                cancelJob(job);
            }, {key: "job-cancel"}));
        }
        if (job.waiting && cli) {
            actions.appendChild(button("Ask the other scan to stop", "", askOtherToStop,
                                       {key: "job-abort-other"}));
        }
        holder.appendChild(actions);

        currentView.update = function (now) {
            title.textContent = J.title(now);
            stage.textContent = J.stage(now.stage, now) +
                (now.state === "cancelling" ? " (stopping at the next safe point)" : "");
            var value = now.total ? fmt.percent(now.done, now.total) : null;
            setProgress(bar, now.waiting ? null : value, {still: now.state !== "running" ||
                                                          now.waiting});
            numbers.textContent = J.progress(now);
            numbers.hidden = !numbers.textContent;
            item.textContent = now.item && now.state === "running" ? "Now: " + now.item : "";
            item.title = now.item || "";
            item.hidden = !item.textContent;
            var parts = [];
            if (now.warnings) {
                parts.push(fmt.plural(now.warnings, "warning"));
            }
            if (now.errors) {
                parts.push(fmt.plural(now.errors, "error"));
            }
            problems.textContent = parts.length ? parts.join(", ") + " so far (see the log)" : "";
            problems.hidden = !parts.length;
            if (now.started) {
                title.title = "Started " + fmt.relative(now.started);
            }
        };
        currentView.update(job);
        restoreFocus(holder, focus);
    }

    function listChanged(name, items) {
        // The relative times ("5 minutes ago") are part of the key, so the
        // lists are redrawn when they change.
        var key = items.map(function (job) {
            return job.id + ":" + job.state + ":" + (job.finished || "") + ":" + job.library +
                ":" + fmt.relative(job.finished || job.created);
        }).join(",");
        if (listKeys[name] === key) {
            return false;
        }
        listKeys[name] = key;
        return true;
    }

    function renderQueue() {
        var queue = snapshot.queue;
        byId("job-queue-panel").hidden = !queue.length;
        if (!listChanged("queue", queue)) {
            return;
        }
        var list = byId("job-queue");
        var focus = focusKey(list);
        clear(list);
        queue.forEach(function (job) {
            list.appendChild(el("li", null, [
                el("div", {className: "job-body"}, [
                    el("div", {className: "job-title", text: J.title(job)}),
                    el("div", {className: "job-meta", text: "Added " + fmt.relative(job.created)})
                ]),
                button("Cancel", "small", function () {
                    jobAction(job, "cancel");
                }, {key: "queue-cancel-" + job.id, title: "Take this job out of the queue"})
            ]));
        });
        restoreFocus(list, focus);
    }

    function stateClass(stateName) {
        return {done: "ok", failed: "bad", cancelled: "", interrupted: "warn"}[stateName] || "";
    }

    function renderRecent() {
        var recent = snapshot.recent;
        if (!listChanged("recent", recent)) {
            return;
        }
        var list = byId("job-recent");
        var focus = focusKey(list);
        clear(list);
        if (!recent.length) {
            list.appendChild(el("li", null, [el("p", {className: "muted",
                                                       text: "No jobs have run yet."})]));
            return;
        }
        recent.forEach(function (job) {
            var meta = [];
            if (job.finished) {
                meta.push(fmt.relative(job.finished));
            }
            if (job.duration) {
                meta.push("took " + fmt.duration(job.duration));
            }
            var summary = J.summary(job);
            if (summary) {
                meta.push(summary);
            }
            var actions = null;
            if (job.state === "interrupted") {
                actions = el("div", {className: "actions"}, [
                    button("Resume", "small primary", function () {
                        jobAction(job, "resume");
                    }, {key: "recent-resume-" + job.id,
                        title: "Run it again; it carries on where it stopped"}),
                    button("Dismiss", "small", function () {
                        jobAction(job, "cancel");
                    }, {key: "recent-dismiss-" + job.id})
                ]);
            }
            // (The command line that does the same: jobs.py log_command.)
            var command = job.command ? copyCommandButton(job.command, "recent-command-" +
                                                          job.id, "Copy command line") : null;
            list.appendChild(el("li", null, [
                el("span", {className: "pill " + stateClass(job.state), text: J.state(job.state)}),
                el("div", {className: "job-body"}, [
                    el("div", {className: "job-title", text: J.title(job)}),
                    meta.length || command ? el("div", {className: "job-meta"}, [
                        meta.join(" \u00b7 "), meta.length && command ? " \u00b7 " : "",
                        command
                    ]) : null,
                    job.error ? el("div", {className: job.state === "interrupted" ? "job-meta"
                        : "job-error", text: job.error}) : null,
                    actions
                ])
            ]));
        });
        restoreFocus(list, focus);
    }

    /* ---------- Log ------------------------------------------------------ */

    function logVisible(line) {
        if (logFilter === "error") {
            return line.level === "error";
        }
        if (logFilter === "warn") {
            return line.level === "warn" || line.level === "error";
        }
        return true;
    }

    // A job's first log line names the command that does the same (jobs.py
    // log_command): shown as code, with a button that copies it.
    var COMMAND_PREFIX = "Command line: ";

    function copyCommandButton(command, key, text, className) {
        return button(text || "Copy", className || "subtle small command-copy", function () {
            SHELL.copyText(command).then(function (ok) {
                SHELL.toast(ok ? "Command line copied." : "The command line could not be " +
                            "copied.", ok ? "ok" : "error", 3);
            });
        }, {key: key, title: "Copy the command line, to run this job again from a " +
            "terminal or a scheduled task (see Automating in the README)"});
    }

    function logNode(line) {
        var message = String(line.message || "");
        var command = message.indexOf(COMMAND_PREFIX) === 0 ?
            message.slice(COMMAND_PREFIX.length) : "";
        return el("div", {className: "log-line " + (line.level || "info") +
                          (command ? " log-command" : "")}, [
            el("span", {className: "log-time", text: fmt.clock(line.time)}),
            command ? [COMMAND_PREFIX, el("code", {text: command}), " ",
                       copyCommandButton(command, "log-copy-" + line.seq)] : message
        ]);
    }

    function addLogLines(lines) {
        if (!lines.length) {
            if (!logLines.length) {
                showEmptyLog();
            }
            return;
        }
        var box = byId("job-log");
        var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
        var empty = box.querySelector(".log-empty");
        if (empty) {
            box.removeChild(empty);
        }
        lines.forEach(function (line) {
            logLines.push(line);
            if (logVisible(line)) {
                box.appendChild(logNode(line));
            }
        });
        if (logLines.length > LOG_LIMIT) {
            logLines = logLines.slice(logLines.length - LOG_LIMIT);
            while (box.childNodes.length > LOG_LIMIT) {
                box.removeChild(box.firstChild);
            }
        }
        if (atBottom) {
            box.scrollTop = box.scrollHeight;
        }
    }

    function showEmptyLog() {
        var box = byId("job-log");
        if (!box.firstChild) {
            box.appendChild(el("div", {className: "log-empty muted",
                                       text: "Messages from jobs appear here."}));
        }
    }

    function renderLog() {
        var box = byId("job-log");
        clear(box);
        logLines.filter(logVisible).forEach(function (line) {
            box.appendChild(logNode(line));
        });
        if (!box.firstChild) {
            box.appendChild(el("div", {className: "log-empty muted", text: logLines.length ?
                "No " + (logFilter === "error" ? "errors" : "warnings") + " so far." :
                "Messages from jobs appear here."}));
        }
        box.scrollTop = box.scrollHeight;
    }

    function setupLogFilter() {
        var buttons = document.querySelectorAll("#job-panel .segmented button");
        Array.prototype.forEach.call(buttons, function (node) {
            node.addEventListener("click", function () {
                logFilter = node.getAttribute("data-filter");
                Array.prototype.forEach.call(buttons, function (other) {
                    other.setAttribute("aria-pressed", other === node ? "true" : "false");
                });
                renderLog();
            });
        });
    }

    /* ---------- Orphans -------------------------------------------------- */

    function renderOrphans() {
        var holder = byId("orphans");
        clear(holder);
        state.orphans.forEach(function (orphan) {
            var counts = orphan.counts || {};
            var box = notice("warn", [
                "Indexed data for \u201c", el("strong", {text: orphan.name}), "\u201d (" +
                fmt.plural(counts.total || 0, "picture") + ", " +
                fmt.number(counts.described || 0) + " described) is not attached to any " +
                "library, so it is not shown in search."
            ], [
                button("Re-attach to...", "small", function () {
                    reattachDialog(orphan);
                }, {key: "orphan-attach-" + orphan.id, disabled: !state.libraries.length,
                    title: state.libraries.length ? "Give this data to one of your libraries"
                        : "Add a library first"}),
                button("Delete...", "small", function () {
                    deleteOrphan(orphan);
                }, {key: "orphan-delete-" + orphan.id})
            ]);
            box.classList.add("orphan");
            holder.appendChild(box);
        });
    }

    function reattachDialog(orphan) {
        var box = SHELL.modal({title: "Re-attach \u201c" + orphan.name + "\u201d"});
        var select = el("select");
        state.libraries.forEach(function (lib) {
            select.appendChild(el("option", {value: lib.id, text: lib.name}));
        });
        var picked = field("Attach to", select, {hint: "Its pictures, descriptions and " +
            "previews become part of this library. This works when the library's folder " +
            "holds the same pictures, for example after removing a library and adding its " +
            "folder again."});
        box.body.appendChild(el("p", {text: "Pictures that the chosen library has indexed " +
            "already (the same file in the same place) cannot be merged; the server says so " +
            "before anything changes."}));
        box.body.appendChild(picked.node);
        box.button("Cancel", "", box.close);
        var go = box.button("Re-attach", "primary", function () {
            go.disabled = true;
            SHELL.admin("/orphans/" + encodeURIComponent(orphan.id) + "/reattach",
                        {library_id: select.value}, {quiet: true}).then(function (reply) {
                box.close();
                SHELL.toast("Moved " + fmt.plural(reply.moved, "picture") + " to " +
                            select.options[select.selectedIndex].text + ".", "ok");
                refreshState();
            }, function (error) {
                go.disabled = false;
                picked.error.textContent = error.message;
                picked.error.hidden = false;
            });
        });
    }

    function deleteOrphan(orphan) {
        var counts = orphan.counts || {};
        SHELL.confirm({
            title: "Delete the indexed data of \u201c" + orphan.name + "\u201d?",
            message: [
                el("p", {text: "This deletes what Image Search stored about " +
                    fmt.plural(counts.total || 0, "picture") + ": " +
                    fmt.number(counts.described || 0) + " descriptions, the thumbnails and " +
                    "the previews. The picture files themselves are not touched."}),
                el("p", {text: "Describing them again takes time (and money, with a cloud " +
                    "service). This cannot be undone."})
            ],
            typeName: orphan.name,
            confirmText: "Delete the data",
            danger: true
        }).then(function (yes) {
            if (!yes) {
                return;
            }
            SHELL.admin("/orphans/" + encodeURIComponent(orphan.id) + "/delete",
                        {confirm_name: orphan.name}).then(function () {
                SHELL.toast("Deleting the data of " + orphan.name + "...", "ok");
                pollJobs();
            });
        });
    }

    /* ---------- Add / edit dialog ---------------------------------------- */

    function endpointOptions(select, current) {
        var seen = {};
        var groups = {};
        var defaultName = state.default_endpoint || "";
        select.appendChild(el("option", {value: "", text: defaultName ?
            "The default endpoint (" + defaultName + ")" : "The default endpoint (none set)"}));
        state.endpoints.forEach(function (endpoint) {
            if (endpoint.group && endpoint.group !== endpoint.name) {
                groups[endpoint.group] = true;
            }
            if (endpoint.enabled === false) {
                return;
            }
            seen[endpoint.name.toLowerCase()] = true;
            var text = endpoint.name + " \u2014 " + (endpoint.preset_label || endpoint.kind) +
                (endpoint.model ? ", " + endpoint.model : " (not set up yet)") +
                (endpoint.local || !endpoint.model ? "" : " (cloud: " + endpoint.host + ")");
            select.appendChild(el("option", {value: endpoint.name, text: text}));
        });
        Object.keys(groups).forEach(function (group) {
            if (!seen[group.toLowerCase()]) {
                seen[group.toLowerCase()] = true;
                select.appendChild(el("option", {value: group, text: "Group " + group +
                                                 " (its endpoints share the work)"}));
            }
        });
        if (current && !seen[current.toLowerCase()]) {
            select.appendChild(el("option", {value: current, text: current +
                                             " (not available: missing or turned off)"}));
        }
        select.value = current || "";
    }

    // "card_or_id" -> "card or ID", "book_page" -> "book page".
    function kindLabel(value) {
        return value === "card_or_id" ? "card or ID" : value.replace(/_/g, " ");
    }

    // The library dialog's refine pass settings: {node, value(), same(value)}.
    // fields.refine gets the server's error message for them.
    function refineSection(lib, fields) {
        var choices = state.refine_choices || {photo_types: [], document_types: [],
                                               defaults: {}};
        var info = (lib && lib.refine_info ? lib.refine_info.settings : choices.defaults) || {};
        var endpoint = el("select");
        endpoint.appendChild(el("option", {value: "", text: "Off (no refine pass)"}));
        var seen = {};
        state.endpoints.forEach(function (item) {
            if (item.enabled === false || !item.model) {
                return;
            }
            seen[item.name.toLowerCase()] = true;
            endpoint.appendChild(el("option", {value: item.name, text: item.name + " \u2014 " +
                item.model + (item.local ? "" : " (cloud: " + item.host + ")")}));
        });
        if (info.endpoint && !seen[info.endpoint.toLowerCase()]) {
            endpoint.appendChild(el("option", {value: info.endpoint, text: info.endpoint +
                                               " (not available: missing or turned off)"}));
        }
        endpoint.value = info.endpoint || "";
        var note = el("span", {className: "field-hint"});
        var endpointField = field("Refine with", endpoint, {after: note, hint: "Usually a " +
            "larger model than the one that describes the library."});

        function boxes(values, chosen) {
            var inputs = {};
            var row = el("div", {className: "check-columns"});
            values.forEach(function (value) {
                var item = check(kindLabel(value), (chosen || []).indexOf(value) >= 0);
                inputs[value] = item.input;
                row.appendChild(item.node);
            });
            return {node: row, inputs: inputs};
        }
        var photoTypes = boxes(choices.photo_types, info.photo_types);
        var documentTypes = boxes(choices.document_types, info.document_types);
        var tokens = el("input", {type: "number", className: "input", min: 500, step: 500,
                                  inputMode: "numeric",
                                  value: String(info.max_output_tokens || "")});
        var tokensField = field("Longest answer (tokens)", tokens, {hint: "Room for the " +
            "transcribed text; " + ((choices.defaults || {}).max_output_tokens || 8000) +
            " by default."});
        var error = el("span", {className: "field-error", hidden: true,
                                attrs: {"aria-live": "polite"}});
        var details = el("div", {className: "refine-details"}, [
            el("div", {className: "field"}, [
                el("span", {className: "field-label", text: "Kinds of picture"}),
                photoTypes.node]),
            el("div", {className: "field"}, [
                el("span", {className: "field-label", text: "Kinds of document"}),
                documentTypes.node]),
            tokensField.node
        ]);
        var node = el("details", {className: "more", open: Boolean(info.endpoint)}, [
            el("summary", {text: "Refine documents and screenshots"}),
            el("p", {className: "field-hint", text: "Pictures of these kinds that are already " +
                "described can be described again by another endpoint, with more room for " +
                "their text: receipts, letters and screenshots get a fuller transcription. " +
                "Start it with Refine documents on the library's card."}),
            endpointField.node, details, error
        ]);
        fields.refine = {node: node, error: error, control: endpoint};

        function chosen(inputs) {
            return Object.keys(inputs).filter(function (key) {
                return inputs[key].checked;
            });
        }

        function update() {
            details.hidden = !endpoint.value;
            var picked = endpointByName(endpoint.value);
            note.textContent = picked && !picked.local ? "Sends these pictures to " +
                picked.host + " (" + picked.preset_label + ")." : "";
            note.className = "field-hint" + (note.textContent ? " result-line bad" : "");
            note.hidden = !note.textContent;
        }
        endpoint.addEventListener("change", update);
        update();

        function value() {
            var number = parseInt(tokens.value, 10);
            return {endpoint: endpoint.value, photo_types: chosen(photoTypes.inputs),
                    document_types: chosen(documentTypes.inputs),
                    max_output_tokens: isFinite(number) ? number : null};
        }

        function same(current) {
            var stored = (lib && lib.refine_info ? lib.refine_info.settings : null) || {};
            if (!current.endpoint && !stored.endpoint) {
                return true;
            }
            function sorted(list) {
                return (list || []).slice().sort();
            }
            return JSON.stringify([current.endpoint, sorted(current.photo_types),
                                   sorted(current.document_types),
                                   current.max_output_tokens]) ===
                JSON.stringify([stored.endpoint || "", sorted(stored.photo_types),
                                sorted(stored.document_types),
                                stored.max_output_tokens || null]);
        }

        return {node: node, value: value, same: same};
    }

    function endpointByName(name) {
        var found = null;
        state.endpoints.forEach(function (endpoint) {
            if (endpoint.name === name) {
                found = endpoint;
            }
        });
        return found;
    }

    function ignoreText(list) {
        return (list || []).join("\n");
    }

    function sameIgnore(text, list) {
        var lines = text.split(/\r?\n/).map(function (line) {
            return line.trim();
        }).filter(Boolean);
        return lines.join("\n") === (list || []).join("\n");
    }

    // options.browse opens the folder picker straight away (?dialog=browse).
    function libraryDialog(lib, options) {
        options = options || {};
        var adding = !lib;
        var box = SHELL.modal({
            title: adding ? "Add a library" : "Edit \u201c" + lib.name + "\u201d",
            wide: true,
            onClose: function () {
                SHELL.setState({dialog: null, id: null});
                window.clearTimeout(pathTimer);
                window.clearTimeout(previewTimer);
            }
        });
        var fields = {};
        var nameTouched = !adding;
        var pathTimer = null;
        var previewTimer = null;
        var checkSeq = 0;
        var previewSeq = 0;
        var found = null;           // {path, count, more} from the last folder check
        var body = box.body;

        // Folder
        var path = el("input", {type: "text", className: "input mono", spellcheck: false,
                                value: lib ? lib.path : "", placeholder: PATH_PLACEHOLDER,
                                autocomplete: "off"});
        var pathResult = el("div", {className: "result-line", hidden: true,
                                    attrs: {"aria-live": "polite"}});
        var relocate = el("span", {className: "field-hint", hidden: true, text: "Moving a " +
            "library to another folder keeps its descriptions: the next scan matches the " +
            "pictures by where they are inside the folder."});
        function chooseFolder(chosen) {
            path.value = chosen;
            pathChanged();
            testPath();
        }
        var browse = button("Browse...", "", function () {
            folderPicker(path.value.trim(), chooseFolder);
        });
        var test = button("Test", "", function () {
            testPath();
        }, {title: "Check that the folder can be read and count its pictures"});
        fields.path = field("Folder", el("div", {className: "field-row"}, [path, browse, test]), {
            div: true,
            control: path,
            hint: "The folder that holds the pictures; its sub-folders are included. It is " +
                "only read, never changed. A network share is typed like \\\\server\\share\\Photos.",
            after: [pathResult, relocate]
        });
        body.appendChild(fields.path.node);

        // Name
        var name = el("input", {type: "text", className: "input", value: lib ? lib.name : "",
                                placeholder: "Taken from the folder name", autocomplete: "off"});
        fields.name = field("Name", name, {hint: "How the library is shown here and when " +
                                           "searching. Each library needs its own name."});
        body.appendChild(fields.name.node);

        // Skipping files
        var ignore = el("textarea", {className: "mono", rows: 3, spellcheck: false,
                                     value: ignoreText(lib ? lib.ignore : []),
                                     placeholder: "Screenshots/*\n*.gif"});
        var previewLine = el("div", {className: "result-line", hidden: true,
                                     attrs: {"aria-live": "polite"}});
        var examples = el("ul", {className: "examples", hidden: true});
        fields.ignore = field("Files and folders to skip", ignore, {
            optional: true,
            hint: "One pattern per line, matched against the path inside the library " +
                "folder: Screenshots/* skips that folder, *.gif skips every GIF file.",
            after: [previewLine, examples]
        });
        var defaults = check("Skip NAS system folders (recommended)",
                             lib ? lib.default_ignores !== false : true,
                             "Thumbnail and recycle-bin folders that NAS boxes and Windows " +
                             "create, such as @eaDir, #recycle and Thumbs.db.");
        fields.default_ignores = {node: defaults.node, error: el("span"), control: defaults.input};
        body.appendChild(el("fieldset", {className: "group"}, [
            el("legend", {text: "Skipping files"}), fields.ignore.node, defaults.node
        ]));

        // Describing
        var newest = radio("order", "Newest pictures first", lib ? lib.newest_first !== false : true);
        var oldest = radio("order", "Oldest pictures first", lib ? lib.newest_first === false : false);
        var order = el("div", {className: "field"}, [
            el("span", {className: "field-label", text: "Describe order"}),
            el("div", {className: "radio-row"}, [newest.node, oldest.node])
        ]);
        var endpoint = el("select");
        endpointOptions(endpoint, lib ? lib.endpoint : "");
        var endpointNote = el("span", {className: "field-hint"});
        fields.endpoint = field("AI model (endpoint)", endpoint, {after: endpointNote, hint:
            "The model that describes this library's pictures. Endpoints are set up on the " +
            "Settings page."});
        var metadata = el("select", null, [
            el("option", {value: "core", text: "Standard (recommended)"}),
            el("option", {value: "extended", text: "Extended"})
        ]);
        metadata.value = lib && lib.metadata === "extended" ? "extended" : "core";
        fields.metadata = field("Detail", metadata, {hint: "Extended also asks for objects, " +
            "landmarks, a location guess, the mood and activities. The answers are about a " +
            "third longer, so describing takes longer and costs more with a cloud service."});
        var notes = el("textarea", {rows: 2, value: lib ? lib.context_notes || "" : "",
                                    placeholder: "Example: Photos from a family farm; the dogs " +
                                                 "are border collies."});
        fields.context_notes = field("Context notes", notes, {optional: true, hint: "Sent with " +
            "every picture to the AI model to help it, and may show up in descriptions. " +
            "Leave out anything private."});
        var refineBox = refineSection(lib, fields);
        body.appendChild(el("fieldset", {className: "group"}, [
            el("legend", {text: "Describing"}), order,
            el("div", {className: "field-grid"}, [fields.endpoint.node, fields.metadata.node]),
            fields.context_notes.node, refineBox.node
        ]));

        // Other
        var previews = check("Build previews (recommended)", lib ? lib.previews !== false : true,
                             "Screen-sized copies for fast viewing, about 0.5 MB per picture, " +
                             "kept in the data folder.");
        var enabled = check("Turned on", lib ? lib.enabled !== false : true,
                            "Turned-off libraries are not searched and Process all skips them.");
        var videos = check("Index videos", lib ? lib.videos !== false : true,
                           "Videos are shown with a poster frame, play in the viewer and are " +
                           "described from a few of their frames. Needs ffmpeg.");
        fields.previews = {node: previews.node, error: el("span"), control: previews.input};
        fields.enabled = {node: enabled.node, error: el("span"), control: enabled.input};
        fields.videos = {node: videos.node, error: el("span"), control: videos.input};
        var other = el("fieldset", {className: "group"}, [
            el("legend", {text: "Other"}), previews.node, videos.node, enabled.node
        ]);
        var scanNow = null;
        if (adding) {
            scanNow = check("Scan and describe it right away", true,
                            "You can also start this later with Scan + describe.");
            other.appendChild(scanNow.node);
        }
        body.appendChild(other);

        var general = el("span", {className: "foot-note field-error", hidden: true});
        box.footer.appendChild(general);
        box.button("Cancel", "", box.close);
        var save = box.button(adding ? "Add library" : "Save changes", "primary", submit);

        function updateEndpointNote() {
            var chosen = endpointByName(endpoint.value || state.default_endpoint || "");
            if (!chosen || !chosen.model) {
                endpointNote.textContent = "No AI model is set up for this yet; add one in " +
                    "Settings before describing. Scanning works without it.";
                endpointNote.className = "field-hint";
            } else if (!chosen.local) {
                endpointNote.textContent = "Sends pictures to " + chosen.host + " (" +
                    chosen.preset_label + "). Their terms of use apply.";
                endpointNote.className = "field-hint result-line bad";
            } else {
                endpointNote.textContent = "";
                endpointNote.className = "field-hint";
            }
            endpointNote.hidden = !endpointNote.textContent;
        }

        // A folder refused when saving: the message goes once the folder
        // is changed or a Test finds it usable, not only on the next save.
        function clearPathError() {
            fields.path.error.hidden = true;
            fields.path.error.textContent = "";
            fields.path.control.removeAttribute("aria-invalid");
        }

        function pathChanged() {
            relocate.hidden = adding || path.value.trim() === lib.path;
            resultLine(pathResult, "");
            clearPathError();
        }

        function testPath() {
            var value = path.value.trim();
            window.clearTimeout(pathTimer);
            if (!value) {
                resultLine(pathResult, "");
                return;
            }
            var seq = ++checkSeq;
            resultLine(pathResult, "Checking the folder...");
            SHELL.admin("/paths/check", {path: value}, {quiet: true}).then(function (reply) {
                if (seq !== checkSeq) {
                    return;
                }
                found = reply.readable && reply.is_dir ?
                    {path: value, count: reply.sample_images, more: reply.more} : null;
                if (reply.problem) {
                    resultLine(pathResult, reply.problem, "bad");
                } else if (reply.readable && reply.is_dir) {
                    clearPathError();
                    var count = reply.more ? "at least " + fmt.plural(reply.sample_images,
                        "picture") + " found (still counting when the check stopped)" :
                        fmt.plural(reply.sample_images, "picture") + " found";
                    resultLine(pathResult, reply.sample_images ? "\u2713 Readable \u00b7 " + count
                        : "Readable, but no pictures were found in it.",
                        reply.sample_images ? "ok" : "");
                } else {
                    resultLine(pathResult, reply.message || "The folder cannot be used.", "bad");
                }
                if (!nameTouched && reply.name) {
                    name.value = reply.name;
                }
                if (!reply.problem && reply.readable) {
                    schedulePreview(0);
                }
            }, function (error) {
                if (seq === checkSeq) {
                    resultLine(pathResult, error.message, "bad");
                }
            });
        }

        function schedulePreview(delay) {
            window.clearTimeout(previewTimer);
            previewTimer = window.setTimeout(runPreview, delay === undefined ? 700 : delay);
        }

        function runPreview() {
            var value = path.value.trim();
            if (!value) {
                resultLine(previewLine, "");
                examples.hidden = true;
                return;
            }
            var request = {ignore: ignore.value, default_ignores: defaults.input.checked};
            if (!adding && value === lib.path) {
                request.library_id = lib.id;
            } else {
                request.path = value;
            }
            var seq = ++previewSeq;
            resultLine(previewLine, "Checking which pictures would be skipped...");
            SHELL.admin("/ignore-preview", request, {quiet: true}).then(function (reply) {
                if (seq !== previewSeq) {
                    return;
                }
                var text = reply.skipped ? fmt.plural(reply.skipped, "picture") + " of the " +
                    fmt.number(reply.scanned) + " checked would be skipped" :
                    "No pictures would be skipped (" + fmt.number(reply.scanned) + " checked)";
                text += reply.truncated ? "; only part of the folder was checked in time." : ".";
                resultLine(previewLine, text, "");
                clear(examples);
                (reply.examples || []).forEach(function (example) {
                    examples.appendChild(el("li", {text: example}));
                });
                examples.hidden = !examples.firstChild;
            }, function (error) {
                if (seq === previewSeq) {
                    resultLine(previewLine, error.message, "");
                    examples.hidden = true;
                }
            });
        }

        function clearErrors() {
            Object.keys(fields).forEach(function (key) {
                fields[key].error.hidden = true;
                fields[key].control.removeAttribute("aria-invalid");
            });
            general.hidden = true;
        }

        function showErrors(error) {
            var shown = false;
            Object.keys(error.fields || {}).forEach(function (key) {
                var target = fields[key];
                if (target) {
                    target.error.textContent = error.fields[key];
                    target.error.hidden = false;
                    target.control.setAttribute("aria-invalid", "true");
                    if (!shown) {
                        target.control.focus();
                    }
                    shown = true;
                }
            });
            if (!shown || !Object.keys(error.fields || {}).length) {
                general.textContent = error.message;
                general.hidden = false;
            }
        }

        function collect() {
            var data = {
                path: path.value.trim(),
                name: name.value.trim(),
                ignore: ignore.value,
                default_ignores: defaults.input.checked,
                newest_first: newest.input.checked,
                endpoint: endpoint.value,
                metadata: metadata.value,
                context_notes: notes.value.trim(),
                previews: previews.input.checked,
                videos: videos.input.checked,
                enabled: enabled.input.checked
            };
            var refineValue = refineBox.value();
            if (adding) {
                if (refineValue.endpoint) {
                    data.refine = refineValue;
                }
                return data;
            }
            if (!refineBox.same(refineValue)) {
                data.refine = refineValue.endpoint ? refineValue : {};
            }
            // Only what changed: a library set up by hand may hold values
            // (such as a relative folder) that this page would not accept.
            var original = {
                path: lib.path, name: lib.name, default_ignores: lib.default_ignores !== false,
                newest_first: lib.newest_first !== false, endpoint: lib.endpoint || "",
                metadata: lib.metadata === "extended" ? "extended" : "core",
                context_notes: lib.context_notes || "", previews: lib.previews !== false,
                videos: lib.videos !== false, enabled: lib.enabled !== false
            };
            Object.keys(original).forEach(function (key) {
                if (data[key] === original[key]) {
                    delete data[key];
                }
            });
            if (sameIgnore(data.ignore, lib.ignore)) {
                delete data.ignore;
            }
            return data;
        }

        function submit() {
            clearErrors();
            var data = collect();
            if (!adding && !Object.keys(data).length) {
                box.close();
                return;
            }
            save.disabled = true;
            save.textContent = adding ? "Adding..." : "Saving...";
            var url = adding ? "/libraries" : "/libraries/" + encodeURIComponent(lib.id);
            SHELL.admin(url, data, {quiet: true}).then(function (reply) {
                box.close();
                var saved = reply.library;
                SHELL.toast(adding ? "Added the library " + saved.name + "." :
                            "Saved the changes to " + saved.name + ".", "ok", 4);
                if (reply.warnings && reply.warnings.length) {
                    showNotice("warn", [el("strong", {text: saved.name + ": "}),
                                        warningList(reply.warnings)]);
                }
                refreshState();
                if (adding && scanNow.input.checked && saved.enabled !== false) {
                    if (saved.endpoint_info && saved.endpoint_info.ready) {
                        queueJob("process", saved, {},
                                 found && found.path === data.path ? found : null);
                    } else {
                        queueJob("scan", saved, {});
                    }
                }
            }, function (error) {
                save.disabled = false;
                save.textContent = adding ? "Add library" : "Save changes";
                if (error.status !== 409) {
                    showErrors(error);
                }
            });
        }

        path.addEventListener("input", function () {
            pathChanged();
            window.clearTimeout(pathTimer);
            pathTimer = window.setTimeout(testPath, 1200);
        });
        path.addEventListener("change", testPath);
        path.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                testPath();
            }
        });
        name.addEventListener("input", function () {
            nameTouched = name.value.trim() !== "";
        });
        ignore.addEventListener("input", function () {
            schedulePreview();
        });
        defaults.input.addEventListener("change", function () {
            schedulePreview(0);
        });
        endpoint.addEventListener("change", updateEndpointNote);
        updateEndpointNote();
        box.submitOnEnter(function () {
            if (!save.disabled) {
                submit();
            }
        });
        if (lib && lib.path) {
            schedulePreview(0);
        }
        if (adding) {
            path.focus();
        }
        if (options.browse) {
            folderPicker("", chooseFolder);
        }
        return box;
    }

    /* ---------- Folder picker -------------------------------------------- */

    function joinPath(folder, child) {
        var windows = /^[A-Za-z]:/.test(folder) || folder.indexOf("\\") >= 0;
        var sep = windows ? "\\" : "/";
        var base = folder.replace(/[\\/]+$/, "");
        return base + sep + child;
    }

    function isFullPath(text) {
        return /^([A-Za-z]:[\\/]|\\\\|\/|~)/.test(text);
    }

    function folderPicker(start, onChoose) {
        var box = SHELL.modal({title: "Choose a folder"});
        var where = el("input", {type: "text", className: "input mono", spellcheck: false,
                                 autocomplete: "off", placeholder: "\\\\server\\share"});
        var go = button("Go", "");
        var up = button("Up one level", "small");
        var top = button("This computer", "small");
        var list = el("ul", {className: "folder-list", attrs: {"aria-label": "Folders"}});
        var hint = el("span", {className: "field-hint"});
        var current = null;
        box.body.appendChild(el("div", {className: "field"}, [
            el("span", {className: "field-label", text: "Folder"}),
            el("div", {className: "field-row"}, [where, go]),
            hint
        ]));
        box.body.appendChild(el("div", {className: "actions"}, [up, top]));
        box.body.appendChild(list);
        box.button("Cancel", "", box.close);
        var choose = box.button("Use this folder", "primary", function () {
            if (current && !current.roots) {
                box.close();
                onChoose(current.path);
            }
        });

        function note(text) {
            list.appendChild(el("li", {className: "note", text: text}));
        }

        function entry(label, target, sub) {
            var item = el("button", {type: "button"}, [
                el("span", {className: "folder-icon", attrs: {"aria-hidden": "true"}}),
                el("span", {text: label}),
                sub ? el("span", {className: "field-hint", text: sub}) : null
            ]);
            item.addEventListener("click", function () {
                open(target);
            });
            list.appendChild(el("li", null, [item]));
        }

        function open(target) {
            clear(list);
            note("Loading...");
            choose.disabled = true;
            SHELL.admin("/fs/list", {path: target}, {quiet: true}).then(function (reply) {
                current = reply;
                clear(list);
                where.value = reply.roots ? "" : reply.path;
                up.disabled = reply.roots;
                top.disabled = reply.roots;
                choose.disabled = reply.roots;
                box.setTitle(reply.roots ? "Choose a folder" : "Choose a folder: " + reply.path);
                if (reply.roots) {
                    reply.dirs.forEach(function (root) {
                        entry(root, root);
                    });
                    if (reply.home) {
                        entry("Your home folder", reply.home, reply.home);
                    }
                } else {
                    reply.dirs.forEach(function (child) {
                        entry(child, joinPath(reply.path, child));
                    });
                    if (!reply.dirs.length) {
                        note("No sub-folders here. Use this folder, or go up a level.");
                    }
                    if (reply.truncated) {
                        note("Only the first " + fmt.number(reply.dirs.length) + " folders are " +
                             "shown; type the rest of the path above.");
                    }
                }
                hint.textContent = reply.hint || "Network shares are not listed: type one " +
                    "above, such as \\\\server\\share, and press Go.";
            }, function (error) {
                clear(list);
                note(error.message);
                up.disabled = !current || current.roots;
                top.disabled = false;
            });
        }

        go.addEventListener("click", function () {
            open(where.value.trim());
        });
        where.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                open(where.value.trim());
            }
        });
        up.addEventListener("click", function () {
            open(current && !current.roots ? current.parent || "" : "");
        });
        top.addEventListener("click", function () {
            open("");
        });
        hint.textContent = "Network shares are not listed: type one above, such as " +
            "\\\\server\\share, and press Go.";
        open(isFullPath(start || "") ? start : "");
        return box;
    }

    /* ---------- Remove dialog -------------------------------------------- */

    function removeDialog(lib) {
        var counts = lib.counts || {};
        var box = SHELL.modal({
            title: "Remove \u201c" + lib.name + "\u201d?",
            onClose: function () {
                SHELL.setState({dialog: null, id: null});
            }
        });
        var amount = fmt.plural(counts.total || 0, "picture") + ", " +
            fmt.number(counts.described || 0) + " described";
        box.body.appendChild(el("p", {text: "The pictures in " + (lib.root || lib.path) +
            " are not touched. Removing the library only changes what Image Search keeps " +
            "about them."}));
        var keep = radio("remove-mode", "Keep the indexed data", true, "The library leaves " +
            "this page and search, but its data (" + amount + ") stays and can be attached " +
            "to a library again later.");
        var drop = radio("remove-mode", "Delete the indexed data", false, [
            "Deletes the descriptions, thumbnails and previews (" + amount + "). ",
            el("strong", {text: "This cannot be undone."})
        ]);
        box.body.appendChild(el("div", {className: "field"}, [keep.node, drop.node]));
        var confirmInput = el("input", {type: "text", className: "input", autocomplete: "off",
                                        spellcheck: false});
        var confirmField = field("Type the library name (" + lib.name + ") to confirm",
                                 confirmInput);
        box.body.appendChild(confirmField.node);
        box.button("Cancel", "", box.close);
        var go = box.button("Remove library", "danger", function () {
            go.disabled = true;
            SHELL.admin("/libraries/" + encodeURIComponent(lib.id) + "/remove",
                        {keep_data: keep.input.checked, confirm_name: confirmInput.value},
                        {quiet: true}).then(function (reply) {
                box.close();
                SHELL.toast("Removed " + lib.name + (reply.kept_data ?
                    "; its data was kept." : reply.job ? "; its data is being deleted." : "."),
                            "ok");
                if (reply.warnings && reply.warnings.length) {
                    showNotice("warn", [el("strong", {text: lib.name + ": "}),
                                        warningList(reply.warnings)]);
                }
                refreshState();
                pollJobs();
            }, function (error) {
                go.disabled = false;
                if (error.status !== 409) {
                    confirmField.error.textContent = error.message;
                    confirmField.error.hidden = false;
                }
            });
        });
        go.disabled = true;
        function update() {
            go.disabled = confirmInput.value.trim().toLowerCase() !== lib.name.trim().toLowerCase();
            go.textContent = drop.input.checked ? "Remove and delete the data" : "Remove library";
        }
        confirmInput.addEventListener("input", update);
        keep.input.addEventListener("change", update);
        drop.input.addEventListener("change", update);
        box.submitOnEnter(function () {
            if (!go.disabled) {
                go.click();
            }
        });
        confirmInput.focus();
        return box;
    }

    /* ---------- Failures dialog ------------------------------------------ */

    function failuresDialog(lib) {
        var box = SHELL.modal({
            title: "Pictures that could not be described: " + lib.name,
            wide: true,
            onClose: function () {
                SHELL.setState({dialog: null, id: null});
            }
        });
        var holder = el("div", {className: "failure-list"}, [el("p", {className: "muted",
                                                                       text: "Loading..."})]);
        box.body.appendChild(holder);
        box.button("Close", "", box.close);
        var retry = box.button("Retry failed", "primary", function () {
            box.close();
            queueJob("describe", lib, {retry_failed: true});
        });
        retry.disabled = lib.enabled === false || !(lib.endpoint_info || {}).ready;
        SHELL.admin("/failures?library=" + encodeURIComponent(lib.id) + "&limit=200")
            .then(function (rows) {
                clear(holder);
                if (!rows.length) {
                    holder.appendChild(el("p", {className: "muted",
                                                text: "No picture has failed."}));
                    retry.disabled = true;
                    return;
                }
                holder.appendChild(el("p", {text: "After 3 failed tries a picture is skipped " +
                    "until you retry. Common causes: the model server stopped, the picture is " +
                    "damaged, or the model gave an answer that could not be read."}));
                var table = el("table", {className: "list"}, [
                    el("thead", null, [el("tr", null, [
                        el("th", {text: "Picture"}), el("th", {text: "Tries"}),
                        el("th", {text: "Error"})
                    ])])
                ]);
                var tbody = el("tbody");
                rows.forEach(function (row) {
                    tbody.appendChild(el("tr", null, [
                        el("td", {className: "mono", text: row.path,
                                  attrs: {"data-label": "Picture"}}),
                        el("td", {text: fmt.number(row.attempts), attrs: {"data-label": "Tries"}}),
                        el("td", {text: row.error || "", attrs: {"data-label": "Error"}})
                    ]));
                });
                table.appendChild(tbody);
                holder.appendChild(el("div", {className: "table-wrap"}, [table]));
            }, function (error) {
                clear(holder);
                holder.appendChild(el("p", {className: "muted", text: error.message}));
            });
        return box;
    }

    /* ---------- Export dialog -------------------------------------------- */

    function copyOptions(options) {
        return JSON.parse(JSON.stringify(options || {}));
    }

    // Three views in one dialog: the options, "Review what will be
    // published" (counts, size, and the flagged pictures to publish or
    // leave out one by one), then the export's progress and result. The
    // options are saved with the library when the export starts.
    // options.step: "review" opens the review straight away. lib may be a
    // trip (tripExportDialog): {id, trip, name, pictures, libraries (names),
    // export_info, video_info}: its pictures are the trip's, and it keeps
    // home private (home_km).
    function exportDialog(lib, options) {
        options = options || {};
        var info = lib.export_info || {};
        var opts = copyOptions(info.options);
        var last = info.last || null;
        var defaultFolder = info.default_output || "";
        var folderValue = last && last.output ? last.output : defaultFolder;
        var closed = false;
        var countTimer = null;
        var countSeq = 0;
        var viewSeq = 0;            // changes with the view: late answers are dropped
        var jobId = null;
        var listener = null;
        var fields = {};
        var refreshCount = function () {};
        var box = SHELL.modal({
            title: "Export \u201c" + lib.name + "\u201d as a website",
            wide: true,
            onClose: function () {
                closed = true;
                window.clearTimeout(countTimer);
                stopListening();
                SHELL.setState({dialog: null, id: null, step: null});
                if (lib.trip) {
                    // (Opened from the trip's page: closed, it goes back there.)
                    backToTrip(lib.trip);
                }
            }
        });
        box.dialog.classList.add("export-dialog");
        var footNote = el("span", {className: "foot-note", attrs: {"aria-live": "polite"}});
        var general = el("span", {className: "foot-note field-error", hidden: true});
        var previewPath = lib.trip ? "/trips/" + encodeURIComponent(lib.trip) +
            "/export/preview" : "/libraries/" + encodeURIComponent(lib.id) + "/export/preview";

        function stopListening() {
            if (listener) {
                snapshotListeners = snapshotListeners.filter(function (item) {
                    return item !== listener;
                });
                listener = null;
            }
        }

        function newView(title) {
            window.clearTimeout(countTimer);
            countSeq += 1;
            viewSeq += 1;
            clear(box.body);
            clear(box.footer);
            fields = {};
            footNote.textContent = "";
            general.hidden = true;
            box.footer.appendChild(footNote);
            box.footer.appendChild(general);
            box.setTitle(title);
            box.body.scrollTop = 0;
        }

        function showGeneral(message) {
            general.textContent = message;
            general.hidden = !message;
        }

        // Ask for the counts again (debounced); then(reply) gets the answer.
        function scheduleCount(then) {
            window.clearTimeout(countTimer);
            var seq = ++countSeq;
            countTimer = window.setTimeout(function () {
                SHELL.admin(previewPath, {options: opts, flagged: false}, {quiet: true})
                    .then(function (reply) {
                        if (!closed && seq === countSeq) {
                            showGeneral("");
                            then(reply);
                        }
                    }, function (error) {
                        if (!closed && seq === countSeq && error.status !== 409) {
                            showGeneral(error.message);
                        }
                    });
            }, COUNT_DELAY);
        }

        function countLine(reply) {
            footNote.textContent = R.headline(reply.counts) + " " + R.sizeText(reply);
        }

        /* -- the options -- */

        function optionsView() {
            SHELL.setState({step: null});
            newView("Export \u201c" + lib.name + "\u201d as a website");
            var body = box.body;
            body.appendChild(el("p", {text: lib.trip ? "This makes a website of the trip " +
                "that works on its own: it opens on the trip's page (its days, map and best " +
                "pictures), with the same search, viewer and picture pages for its pictures, " +
                "no server and nothing to install. Put it on any web host, or open it from " +
                "the folder. Nothing is uploaded; the files are written to a folder on this " +
                "computer." : "This makes a copy of the library that works as " +
                "a website on its own: the same search, viewer and picture pages, with no " +
                "server and nothing to install. Put it on any web host, or open it from the " +
                "folder. Nothing is uploaded; the files are written to a folder on this " +
                "computer."}));

            // Which pictures: the whole library, or the pictures of one album.
            var albumSelect = el("select", {className: "input export-album"});
            albumSelect.appendChild(el("option", {value: "", text: "The whole library"}));
            if (opts.album) {
                albumSelect.appendChild(el("option", {value: String(opts.album),
                                                      text: "The album chosen before"}));
                albumSelect.value = String(opts.album);
            }
            // (A trip's export is of the trip's pictures: no album.)
            var albumList = lib.trip ? Promise.reject(null)
                : SHELL.admin("/albums", undefined, {quiet: true});
            albumList.then(function (reply) {
                var chosen = albumSelect.value;
                while (albumSelect.options.length > 1) {
                    albumSelect.remove(1);
                }
                (reply.albums || []).forEach(function (album) {
                    if (album.kind !== "manual") {
                        return;
                    }
                    albumSelect.appendChild(el("option", {
                        value: String(album.id),
                        text: album.name + " (" + fmt.plural(album.count || 0, "picture") + ")"}));
                });
                albumSelect.value = chosen;
                if (albumSelect.value !== chosen) {
                    // (The album is gone: the whole library, as the export would refuse it.)
                    albumSelect.value = "";
                    changed();
                }
            }, function () {});
            albumSelect.addEventListener("change", changed);
            fields.album = field(R.OPTIONS.album.label, albumSelect, {hint: R.OPTIONS.album.hint});
            body.appendChild(el("fieldset", {className: "group"}, [
                el("legend", {text: "Which pictures"}), lib.trip ? el("p", {
                    text: "The trip's " + fmt.plural(lib.pictures || 0, "picture") +
                        ((lib.libraries || []).length ? " (in " + lib.libraries.join(", ") +
                         ")" : "") + ", less what is left out below. The names of the " +
                        "libraries are not published."}) : fields.album.node
            ]));

            // Pictures
            var modes = {};
            var modeList = el("div", {className: "export-choices"});
            ["web", "original", "none"].forEach(function (mode) {
                var words = R.OPTIONS.images[mode];
                var choice = radio("export-images-" + lib.id, words.label, opts.images === mode,
                                   words.hint);
                choice.input.value = mode;
                choice.input.addEventListener("change", changed);
                modes[mode] = choice.input;
                modeList.appendChild(choice.node);
            });
            fields.images = {error: el("span", {className: "field-error", hidden: true}),
                             control: modes.web};
            // Videos: their poster frame always; the videos themselves when chosen.
            var withVideos = null;
            var hasVideos = Boolean((lib.video_info || {}).count) || opts.videos === "include" ||
                (opts.exclude_types || []).indexOf("video") >= 0;
            if (hasVideos) {
                withVideos = check(R.OPTIONS.videos.label, opts.videos === "include",
                                   R.OPTIONS.videos.hint.replace("{mb}",
                                                                 String(opts.max_video_mb)));
                withVideos.input.addEventListener("change", changed);
            }
            fields.videos = {error: el("span", {className: "field-error", hidden: true}),
                             control: withVideos ? withVideos.input : modes.web};
            // Camera RAW files: never published unless chosen; offered once the
            // count finds pictures with one (or when chosen before).
            var withRaws = check(R.OPTIONS.raw_files.label, opts.raw_files,
                                 R.OPTIONS.raw_files.hint);
            withRaws.input.addEventListener("change", changed);
            withRaws.node.hidden = !opts.raw_files;
            function rawAvailability(raws) {
                if (raws && raws.total) {
                    withRaws.node.hidden = false;
                }
            }
            body.appendChild(el("fieldset", {className: "group"}, [
                el("legend", {text: "Pictures"}), modeList,
                withVideos ? withVideos.node : null, withRaws.node, fields.images.error,
                fields.videos.error
            ]));

            // What is left out
            var leave = {};
            var leaveList = el("div", {className: "export-choices two"});
            var leaveItems = [["exclude_private", R.OPTIONS.exclude_private],
                              ["exclude_sensitive", R.OPTIONS.exclude_sensitive],
                              ["document", R.TYPES.document],
                              ["screenshot", R.TYPES.screenshot], ["scan", R.TYPES.scan],
                              hasVideos ? ["video", R.TYPES.video] : null,
                              ["exclude_undescribed", R.OPTIONS.exclude_undescribed]];
            leaveItems.filter(Boolean).forEach(function (item) {
                var key = item[0];
                var on = R.TYPES[key] ? (opts.exclude_types || []).indexOf(key) >= 0 : opts[key];
                leave[key] = check(item[1].label, on, item[1].hint);
                leave[key].input.addEventListener("change", changed);
                leaveList.appendChild(leave[key].node);
            });
            fields.exclude_types = {error: el("span", {className: "field-error", hidden: true}),
                                    control: leave.document.input};
            body.appendChild(el("fieldset", {className: "group"}, [
                el("legend", {text: "Leave out"}),
                leaveList,
                el("p", {className: "field-hint", text: "These come from the AI model's " +
                    "descriptions, which can be wrong. The next step shows the pictures they " +
                    "catch, to publish or leave out one by one. Hidden pictures are never " +
                    "published."}),
                fields.exclude_types.error
            ]));

            // What each picture carries
            var details = {};
            var detailList = el("div", {className: "export-choices two"});
            // (Estimated places, drive routes and street maps go only with the location.)
            function estimatedAvailability() {
                details.estimated_places.input.disabled = !details.location.input.checked;
                details.routes.input.disabled = !details.location.input.checked;
                streetsAvailability(null);
                // (So does a trip's story, which names its places.)
                if (details.story && ((lib.export_info || {}).story || {}).available) {
                    details.story.input.disabled = !details.location.input.checked;
                }
            }
            // (People's names only where there are people, or the option is on.)
            var people = (state && state.people) || {};
            // (A trip's export: its story too, trip_stories.py.)
            var detailKeys = ["file_names", "folders", "location", "estimated_places", "routes",
                              "street_maps", "similar", "meaning", "looks", "marks",
                              "people_names"]
                .concat(lib.trip ? ["story"] : []);
            detailKeys.filter(function (key) {
                return key !== "people_names" || people.stored || people.enabled ||
                    opts.people_names;
            }).forEach(function (key) {
                details[key] = check(R.OPTIONS[key].label, opts[key], R.OPTIONS[key].hint);
                if (key === "location" || key === "street_maps") {
                    details[key].input.addEventListener("change", estimatedAvailability);
                }
                details[key].input.addEventListener("change", changed);
                detailList.appendChild(details[key].node);
            });
            // Street maps: how far around each place, and how detailed (when on).
            var streetKm = el("select", {className: "input export-street-km"});
            [2, 5, 10, 20].forEach(function (km) {
                streetKm.appendChild(el("option", {value: String(km), text: km + " km"}));
            });
            var streetZoom = el("select", {className: "input export-street-zoom"});
            [[15, "Buildings (zoom 15)"], [14, "Streets (zoom 14)"],
             [12, "Main roads (zoom 12)"]].forEach(function (pair) {
                streetZoom.appendChild(el("option", {value: String(pair[0]), text: pair[1]}));
            });
            [[streetKm, opts.street_km], [streetZoom, opts.street_zoom]].forEach(function (pair) {
                if (!pair[0].querySelector('option[value="' + Number(pair[1]) + '"]')) {
                    pair[0].appendChild(el("option", {value: String(pair[1]),
                                                      text: String(pair[1])}));
                }
                pair[0].value = String(pair[1]);
                pair[0].addEventListener("change", changed);
            });
            var streetsRow = el("div", {className: "field-grid export-streets"}, [
                field(R.OPTIONS.street_km.label, streetKm, {hint: R.OPTIONS.street_km.hint})
                    .node,
                field(R.OPTIONS.street_zoom.label, streetZoom,
                      {hint: R.OPTIONS.street_zoom.hint}).node
            ]);
            var streetsReason = el("span", {className: "field-hint field-reason", hidden: true});
            details.street_maps.node.lastChild.appendChild(streetsReason);
            var streetsKnown = null;
            function streetsAvailability(found) {
                if (found) {
                    streetsKnown = found;
                }
                var input = details.street_maps.input;
                var downloaded = !streetsKnown || streetsKnown.available !== false;
                input.disabled = !details.location.input.checked || !downloaded;
                streetsReason.textContent = downloaded ? "" : "Not available: no street maps " +
                    "were downloaded (Manage page, Street maps).";
                streetsReason.hidden = downloaded;
                streetsRow.hidden = input.disabled || !input.checked;
            }
            estimatedAvailability();
            // The trip's story: only when it has one that is not hidden (else off, with
            // the reason; the saved choice is kept).
            if (details.story) {
                var story = (lib.export_info || {}).story || {};
                if (!story.available) {
                    details.story.input.disabled = true;
                    details.story.input.checked = false;
                    details.story.node.lastChild.appendChild(el("span", {
                        className: "field-hint field-reason",
                        text: story.hidden ? "Not available: the story is hidden (show it on " +
                            "the trip's page)." : "Not available: the trip has no story yet " +
                            "(write it on the trip's page)."}));
                }
            }
            // Home (a trip's export): the squares around it that nothing shows. A
            // choice of the sizes the server takes (trip_export.HOME_KM_STEPS), so
            // that "off" is only ever chosen, never what an empty field means.
            var homeKm = null;
            var homeOff = null;
            if (lib.trip) {
                homeKm = el("select", {className: "input export-home-km"});
                HOME_KM_STEPS.forEach(function (km) {
                    homeKm.appendChild(el("option", {value: String(km),
                                                     text: "About " + km + " km"}));
                });
                homeKm.appendChild(el("option", {value: "0", text: "Off: home is not kept out"}));
                homeKm.value = String(HOME_KM_STEPS.indexOf(opts.home_km) >= 0 ||
                                      opts.home_km === 0 ? opts.home_km : HOME_KM_DEFAULT);
                homeOff = notice("warn", "Home is not kept out: positions, routes and towns " +
                                 "near it, and the dashcam clips filmed there, are published " +
                                 "like any others.");
                homeOff.hidden = homeKm.value !== "0";
                homeKm.addEventListener("change", function () {
                    homeOff.hidden = homeKm.value !== "0";
                    changed();
                });
                fields.home_km = field(R.OPTIONS.home_km.label, homeKm, {
                    div: true, after: homeOff,
                    hint: R.OPTIONS.home_km.hint + ((lib.export_info || {}).home === false
                        ? " No home was found among the pictures: there is nothing to keep " +
                          "out." : "")});
            }
            // Search by meaning in the browser: on (as saved) only when it can
            // go; otherwise off, with the reason (the saved choice is kept).
            var meaningReason = el("span", {className: "field-hint field-reason", hidden: true});
            details.meaning.node.lastChild.appendChild(meaningReason);
            function meaningAvailability(meaning) {
                var input = details.meaning.input;
                var available = !meaning || meaning.available !== false;
                input.disabled = !available;
                input.checked = available && Boolean(opts.meaning);
                meaningReason.textContent = available ? "" : "Not available: " +
                    (meaning.reason || "search by meaning is not set up.");
                meaningReason.hidden = available;
            }
            meaningAvailability((lib.export_info || {}).meaning);
            var detailsLine = el("p", {className: "field-hint"});
            body.appendChild(el("fieldset", {className: "group"}, [
                el("legend", {text: "Also publish"}),
                detailList,
                streetsRow,
                fields.home_km ? fields.home_km.node : null,
                detailsLine,
                el("p", {className: "field-hint", text: "Never published: where the pictures " +
                    "are on this computer, file dates and times (a picture without a date " +
                    "from the camera or its file name is published undated), the AI model " +
                    "and what it cost, your other libraries and your settings."})
            ]));

            // The site
            var title = el("input", {type: "text", className: "input", value: opts.title || "",
                                     placeholder: lib.name, autocomplete: "off"});
            fields.title = field(R.OPTIONS.title.label, title, {optional: true,
                hint: R.OPTIONS.title.hint + (lib.trip ? " Blank: the trip's name."
                                              : " Blank: the library name.")});
            var credits = el("input", {type: "text", className: "input",
                                       value: opts.credits || "", autocomplete: "off"});
            fields.credits = field(R.OPTIONS.credits.label, credits, {optional: true,
                hint: R.OPTIONS.credits.hint});
            var folder = el("input", {type: "text", className: "input mono", spellcheck: false,
                                      value: folderValue, placeholder: defaultFolder,
                                      autocomplete: "off"});
            var browse = button("Browse...", "", function () {
                folderPicker(folder.value.trim(), function (chosen) {
                    folder.value = chosen;
                });
            });
            fields.out_dir = field("Folder", el("div", {className: "field-row"},
                                                [folder, browse]), {
                div: true,
                control: folder,
                hint: "A new or empty folder, or one an earlier export wrote: the files that " +
                    "export made are updated or removed, and nothing else in it is touched." +
                    (folderValue !== defaultFolder ? " The default folder is " + defaultFolder +
                     "." : "")
            });
            var zip = check(R.OPTIONS.zip.label, opts.zip, R.OPTIONS.zip.hint);
            var indexing = check(R.OPTIONS.allow_indexing.label, opts.allow_indexing,
                                 R.OPTIONS.allow_indexing.hint);
            zip.input.addEventListener("change", changed);
            indexing.input.addEventListener("change", changed);
            body.appendChild(el("fieldset", {className: "group"}, [
                el("legend", {text: "The site"}),
                el("div", {className: "field-grid"}, [fields.title.node, fields.credits.node]),
                fields.out_dir.node,
                el("div", {className: "export-choices two"}, [zip.node, indexing.node])
            ]));

            function collect() {
                var result = copyOptions(opts);
                Object.keys(modes).forEach(function (mode) {
                    if (modes[mode].checked) {
                        result.images = mode;
                    }
                });
                var types = (opts.exclude_types || []).filter(function (type) {
                    return !R.TYPES[type];
                });
                Object.keys(R.TYPES).forEach(function (type) {
                    // (A kind not offered here stays as saved.)
                    if (leave[type] ? leave[type].input.checked
                        : (opts.exclude_types || []).indexOf(type) >= 0) {
                        types.push(type);
                    }
                });
                result.exclude_types = types;
                ["exclude_private", "exclude_sensitive", "exclude_undescribed"].forEach(
                    function (key) {
                        result[key] = leave[key].input.checked;
                    });
                Object.keys(details).forEach(function (key) {
                    // (A choice that cannot be made now keeps its saved value.)
                    result[key] = details[key].input.disabled ? Boolean(opts[key])
                        : details[key].input.checked;
                });
                result.title = title.value.trim();
                result.credits = credits.value.trim();
                result.album = albumSelect.value ? Number(albumSelect.value) : null;
                result.zip = zip.input.checked;
                result.allow_indexing = indexing.input.checked;
                if (withVideos) {
                    result.videos = withVideos.input.checked ? "include" : "skip";
                }
                if (homeKm) {
                    var km = Number(homeKm.value);
                    result.home_km = km === 0 || HOME_KM_STEPS.indexOf(km) >= 0 ? km
                        : HOME_KM_DEFAULT;
                }
                result.raw_files = withRaws.input.checked;
                result.street_km = Number(streetKm.value) || opts.street_km;
                result.street_zoom = Number(streetZoom.value) || opts.street_zoom;
                return result;
            }

            function changed() {
                opts = collect();
                detailsLine.textContent = R.detailsText(opts);
                refreshCount();
            }

            refreshCount = function () {
                scheduleCount(function (reply) {
                    meaningAvailability((reply.counts || {}).meaning);
                    rawAvailability((reply.counts || {}).raws);
                    streetsAvailability((reply.counts || {}).street_maps);
                    countLine(reply);
                });
            };
            detailsLine.textContent = R.detailsText(opts);
            footNote.textContent = "Counting the pictures...";
            refreshCount();
            title.addEventListener("input", function () {
                opts = collect();
            });
            credits.addEventListener("input", function () {
                opts = collect();
            });
            box.button("Cancel", "", box.close);
            box.button("Review what will be published", "primary", function () {
                opts = collect();
                folderValue = folder.value.trim();
                reviewView();
            });
        }

        function showOptionErrors(error) {
            var shown = false;
            Object.keys(error.fields || {}).forEach(function (key) {
                var target = fields[key];
                if (target && target.error) {
                    target.error.textContent = error.fields[key];
                    target.error.hidden = false;
                    if (target.control) {
                        target.control.setAttribute("aria-invalid", "true");
                        if (!shown) {
                            target.control.focus();
                        }
                    }
                    shown = true;
                }
            });
            if (!shown) {
                showGeneral(error.message);
            }
        }

        /* -- the review -- */

        // The review: what the server counted once, then the choices made
        // picture by picture are counted here (R.recount), with no new
        // request per click.
        function reviewView() {
            SHELL.setState({step: "review"});
            newView("Review what will be published: " + lib.name);
            var summary = el("div", {className: "export-summary"}, [
                el("p", {className: "muted", text: "Looking at the pictures..."})
            ]);
            var holder = el("div", {className: "export-review"});
            box.body.appendChild(summary);
            box.body.appendChild(holder);
            box.button("Back", "", optionsView);
            var go = box.button("Export", "primary", startExport);
            go.disabled = true;
            var base = null;

            function update(reply) {
                countLine(reply);
                renderSummary(summary, reply);
                go.disabled = !reply.counts.exported;
                go.textContent = reply.counts.exported ? "Export " +
                    fmt.plural(reply.counts.exported, "picture") : "Export";
            }
            refreshCount = function () {
                if (base) {
                    update(R.recount(base, opts));
                }
            };
            var mine = viewSeq;
            var ask = {options: opts, out_dir: folderValue || ""};
            SHELL.admin(previewPath, ask, {quiet: true}).then(function (reply) {
                if (closed || mine !== viewSeq) {
                    return;
                }
                base = reply;
                opts = copyOptions(reply.options);
                update(reply);
                renderGroups(holder, reply.flagged || []);
            }, function (error) {
                if (closed || mine !== viewSeq || error.status === 409) {
                    return;
                }
                clear(summary);
                summary.appendChild(notice("error", error.message));
                if (Object.keys(error.fields || {}).length) {
                    optionsView();
                    showOptionErrors(error);
                }
            });
        }

        function renderSummary(holder, reply) {
            clear(holder);
            holder.appendChild(el("p", {className: "export-headline",
                                        text: R.headline(reply.counts)}));
            var size = R.sizeText(reply);
            if (size) {
                holder.appendChild(el("p", {className: "muted", text: size}));
            }
            var left = R.leftOut(reply.counts);
            if (left.length) {
                holder.appendChild(el("div", {className: "export-left"}, [
                    el("span", {className: "export-left-label", text: "Left out:"}),
                    el("ul", {className: "stats"}, left.map(function (text) {
                        return el("li", {className: "stat", text: text});
                    }))
                ]));
            }
            holder.appendChild(el("p", {className: "export-details",
                                        text: R.detailsText(opts)}));
            var similarLine = R.similarText(reply.counts, opts);
            if (similarLine) {
                holder.appendChild(el("p", {className: "export-details", text: similarLine}));
            }
            var looksLine = R.looksText(reply.counts, opts, reply.size_parts);
            if (looksLine) {
                holder.appendChild(el("p", {className: "export-details", text: looksLine}));
            }
            var meaningLine = R.meaningText(reply.counts, opts);
            if (meaningLine) {
                holder.appendChild(el("p", {className: "export-details", text: meaningLine}));
            }
            var counts = reply.counts || {};
            var videoNote = R.videoText(counts.videos, opts);
            if (videoNote) {
                holder.appendChild(el("p", {className: "muted", text: videoNote}));
            }
            var rawNote = R.rawsText(counts.raws, opts);
            if (rawNote) {
                holder.appendChild(opts.raw_files && (counts.raws || {}).included
                    ? notice("warn", rawNote) : el("p", {className: "muted", text: rawNote}));
            }
            var drivesNote = R.drivesText(counts);
            if (drivesNote) {
                holder.appendChild(el("p", {className: "muted", text: drivesNote}));
            }
            var routesNote = R.routesText(counts);
            if (routesNote) {
                holder.appendChild(el("p", {className: "muted", text: routesNote}));
            }
            var streetsNote = R.streetsText(counts, opts);
            if (streetsNote) {
                holder.appendChild(el("p", {className: "muted", text: streetsNote}));
            }
            var overlayNote = R.overlayText(counts);
            if (overlayNote) {
                holder.appendChild(notice("warn", overlayNote));
            }
            if (counts.unavailable) {
                holder.appendChild(notice("error", (lib.trip ? "A library folder of the " +
                    "trip" : "The folder of " + lib.name) + " cannot be " +
                    "read right now (offline?), and " + fmt.plural(counts.unavailable,
                    "picture") + " to publish " + (counts.unavailable === 1 ? "needs" :
                    "need") + " it (" + (opts.images === "original" ? "originals are read " +
                    "from it" : "no web-size version is kept for them") + "). Connect it " +
                    "first" + (opts.images === "original" ? ", or go back and choose Web " +
                    "size" : "") + "; otherwise they are left out."));
            }
            if (typeof reply.free_bytes === "number" && typeof reply.size_estimate === "number" &&
                    reply.free_bytes < reply.size_estimate) {
                holder.appendChild(notice("warn", "The disk of the export folder has " +
                    R.bytes(reply.free_bytes) + " free, and the export takes about " +
                    R.bytes(reply.size_estimate) + " (less when it updates an earlier export " +
                    "there). Free some space, or go back and choose another folder or " +
                    "smaller pictures."));
            }
            holder.appendChild(notice("warn", "Descriptions, tags and the text read from " +
                "pictures are published as the AI model wrote them, and may name people or " +
                "places. Open a picture (it opens in a new tab) to read what it says about it."));
        }

        function renderGroups(holder, flagged) {
            clear(holder);
            var groups = R.groups(flagged);
            if (!groups.length) {
                holder.appendChild(el("p", {className: "muted", text: "No picture was " +
                    "flagged: every picture the options allow is published as it is."}));
                return;
            }
            holder.appendChild(el("p", {className: "field-hint", text: "The pictures the AI " +
                "model flagged. Untick Publish to leave one out, or tick it to publish one " +
                "the options leave out."}));
            groups.forEach(function (group) {
                var grid = el("ul", {className: "export-grid"});
                var more = button("", "small", null);
                var shown = 0;
                function showMore() {
                    group.items.slice(shown, shown + REVIEW_PAGE).forEach(function (item) {
                        grid.appendChild(reviewTile(item));
                    });
                    shown = Math.min(group.items.length, shown + REVIEW_PAGE);
                    var rest = group.items.length - shown;
                    more.hidden = rest <= 0;
                    more.textContent = "Show " + fmt.number(Math.min(rest, REVIEW_PAGE)) +
                        " more of " + fmt.number(rest);
                }
                more.addEventListener("click", showMore);
                holder.appendChild(el("section", {className: "export-group"}, [
                    el("h3", {text: group.title + " (" + fmt.number(group.items.length) + ")"}),
                    group.hint ? el("p", {className: "field-hint", text: group.hint}) : null,
                    grid,
                    more
                ]));
                showMore();
            });
        }

        function reviewTile(item) {
            var input = el("input", {type: "checkbox", checked: R.published(item, opts)});
            var name = item.title || "Picture without a title";
            var node = el("li", {className: "export-tile"}, [
                el("a", {className: "export-thumb", href: SITE.detailUrl(item.id),
                         target: "_blank", rel: "noopener",
                         title: "Open \u201c" + name + "\u201d in a new tab"}, [
                    el("img", {src: SITE.thumbUrl(item), alt: name, loading: "lazy"})
                ]),
                el("div", {className: "export-tile-title", text: name, title: name}),
                el("div", {className: "export-tile-reasons", text: R.reasonWords(item.reasons)}),
                el("label", {className: "check"}, [input, el("span", {text: "Publish"})])
            ]);
            // Chosen to be published before it got a flag that leaves it out.
            var note = el("div", {className: "export-tile-note"});
            node.insertBefore(note, node.lastChild);
            function paint() {
                node.classList.toggle("is-out", !input.checked);
                var added = R.newFlags(item, opts);
                note.textContent = added.length ? "New since you chose to publish it: " +
                    R.reasonWords(added) : "";
                note.hidden = !added.length;
            }
            input.addEventListener("change", function () {
                opts = R.choose(opts, item, input.checked);
                paint();
                refreshCount();
            });
            paint();
            return node;
        }

        /* -- the export -- */

        function startExport() {
            var go = this;
            go.disabled = true;
            showGeneral("");
            var params = {options: opts, save_options: true};
            if (folderValue && folderValue !== defaultFolder) {
                params.out_dir = folderValue;
            }
            if (lib.trip) {
                params.trip = lib.trip;
            }
            SHELL.admin("/jobs", {kind: "export", library_id: lib.trip ? null : lib.id,
                                  params: params},
                        {quiet: true}).then(function (reply) {
                if (!reply.added) {
                    SHELL.toast("An export of " + lib.name + " is already running; it " +
                                "continues.", "info", 6);
                }
                jobId = reply.job.id;
                applySnapshot(reply.jobs);
                refreshState();
                if (!closed) {
                    progressView(reply.job);
                }
            }, function (error) {
                go.disabled = false;
                if (closed || error.status === 409) {
                    return;
                }
                if (Object.keys(error.fields || {}).length) {
                    optionsView();
                    showOptionErrors(error);
                } else {
                    showGeneral(error.message);
                }
            });
        }

        /* -- progress and result -- */

        function progressView(job) {
            SHELL.setState({step: null});
            newView("Exporting \u201c" + lib.name + "\u201d");
            jobId = job.id;
            var status = el("p", {className: "export-status"});
            var bar = progressBar(null, {label: "Export progress"});
            var numbers = el("p", {className: "muted"});
            var current = job;
            box.body.appendChild(status);
            box.body.appendChild(bar);
            box.body.appendChild(numbers);
            box.body.appendChild(el("p", {className: "muted", text: "You can close this " +
                "window: the export carries on, and the job panel shows it."}));
            box.button("Stop export", "", function () {
                cancelJob(current);
            });
            box.button("Close", "", box.close);
            function update(now) {
                current = now;
                if (now.finished) {
                    finishedView(now);
                    return;
                }
                var other = snapshot && snapshot.current && snapshot.current.id !== now.id;
                status.textContent = now.state === "queued" && !now.waiting ? (other ?
                    "Waiting for the running job to finish..." : "Starting...") :
                    J.stage(now.stage, now) + (now.state === "paused" ? " (paused)" :
                                               now.waiting ? "" : "...");
                var value = now.total ? fmt.percent(now.done, now.total) : null;
                setProgress(bar, value, {still: now.state !== "running"});
                numbers.textContent = J.progress(now);
            }
            stopListening();
            listener = function (data) {
                var now = jobById(data, jobId);
                if (now && !closed) {
                    update(now);
                }
            };
            snapshotListeners.push(listener);
            update(job);
            if (snapshot) {
                listener(snapshot);
            }
        }

        function finishedView(job) {
            stopListening();
            refreshState();
            SHELL.setState({step: "done"});
            var summary = job.summary || {};
            if (job.state !== "done") {
                newView(job.state === "failed" ? "The export failed" : "The export stopped");
                box.body.appendChild(notice(job.state === "failed" ? "error" : "warn",
                    job.state === "failed" ? job.error || "The export could not be made." :
                    "The export was stopped before it finished. What it wrote is kept; " +
                    "export again to finish it."));
                box.button("Close", "", box.close);
                box.button("Back to the options", "primary", optionsView);
                return;
            }
            newView("\u201c" + lib.name + "\u201d is exported");
            var output = summary.output || "";
            var lines = ["Exported " + fmt.plural(summary.count || 0, "picture") +
                (summary.bytes ? " (" + fmt.bytes(summary.bytes) + " in all)" : "") + "."];
            if (!summary.count) {
                lines.push("The site has no pictures: every picture was left out. Go back to " +
                           "the options to see why.");
            }
            if (summary.failed) {
                lines.push(fmt.plural(summary.failed, "picture") + " could not be read and " +
                           (summary.failed === 1 ? "was" : "were") + " left out; the log " +
                           "names them.");
            }
            if (summary.kept) {
                lines.push(fmt.plural(summary.kept, "picture") + " could not be read; the " +
                           "files of the earlier export were kept.");
            }
            box.body.appendChild(notice(!summary.count || summary.failed || summary.kept
                ? "warn" : "ok", lines.join(" ")));
            box.body.appendChild(el("div", {className: "field"}, [
                el("span", {className: "field-label", text: "Folder"}),
                el("div", {className: "export-copy"}, [
                    el("code", {className: "export-path", text: output}),
                    button("Copy", "subtle small", function () {
                        SHELL.copyText(output).then(function (ok) {
                            SHELL.toast(ok ? "Folder path copied." : "The path could not be " +
                                        "copied.", ok ? "ok" : "error", 3);
                        });
                    })
                ]),
                summary.zip ? el("span", {className: "field-hint", text: "Zip file: " +
                                          summary.zip}) : null
            ]));
            var command = R.serveCommand(output, summary);
            box.body.appendChild(el("h3", {className: "export-h3", text: "Putting it on the web"}));
            box.body.appendChild(el("ol", {className: "export-steps"}, [
                el("li", {text: "Copy everything in the folder to a web host that serves " +
                    "plain files, such as GitHub Pages, Netlify or your own web server. " +
                    "Nothing needs to be installed or run there."}),
                el("li", null, [
                    "To try it on this computer first, run this command, then open " +
                    "http://localhost:8000/ in a browser:",
                    el("div", {className: "export-copy"}, [
                        el("code", {className: "export-path", text: command}),
                        button("Copy", "subtle small", function () {
                            SHELL.copyText(command).then(function (ok) {
                                SHELL.toast(ok ? "Command copied." : "The command could " +
                                            "not be copied.", ok ? "ok" : "error", 3);
                            });
                        })
                    ])
                ]),
                el("li", {text: "Or double-click index.html in the folder. Opened that way, " +
                    "360-degree photos are shown flat."}),
                el("li", {text: "After describing more pictures, export again: only what " +
                    "changed is written, and pictures left out since are removed."})
            ]));
            if (job.command) {
                // (The same export as a command, for a scheduled task: jobs.py.)
                box.body.appendChild(el("div", {className: "field"}, [
                    el("span", {className: "field-label", text: "Command line"}),
                    el("div", {className: "export-copy"}, [
                        el("code", {className: "export-path", text: job.command}),
                        copyCommandButton(job.command, "export-command", "Copy", "subtle small")
                    ]),
                    el("span", {className: "field-hint", text: "Run in the program folder, " +
                        "it exports again with these options (it names the folder of the " +
                        "settings with --settings-base when they are not in the program " +
                        "folder): a scheduled task can do that. See Automating in the README."})
                ]));
            }
            if (!summary.count || summary.failed) {
                box.button("Back to the options", "", optionsView);
            }
            if (lib.trip) {
                box.button("Back to the trip", "", box.close);
            }
            box.button("Close", "", box.close);
            box.button("Open folder", "primary", function () {
                openExportFolder(lib);
            });
        }

        var running = exportJobOf(lib);
        var finished = options.step === "done" ? lastExportJob(lib) : null;
        if (options.step === "done" && last && last.finished &&
                (!finished || finished.state !== "done" ||
                 (finished.summary || {}).output !== last.output)) {
            // Made from the command line, or the job is no longer listed.
            finished = {state: "done", summary: {output: last.output, count: last.pictures,
                                                 bytes: last.bytes, zip: last.zip}};
        }
        if (running) {
            progressView(running);
        } else if (finished) {
            finishedView(finished);
        } else if (options.step === "review") {
            reviewView();
        } else {
            optionsView();
        }
        return box;
    }

    /* ---------- Dialogs from the URL ------------------------------------- */

    // The sizes of the area around home a trip's export can keep out
    // (trip_export.HOME_KM_STEPS), and the one it keeps out by default.
    var HOME_KM_STEPS = [5, 10, 20, 40, 80, 160];
    var HOME_KM_DEFAULT = 10;

    // Back to a trip's page (from its Export dialog): back in the history when the
    // trip's page opened it, else to the page.
    function backToTrip(tripId) {
        var page = "/trip/" + encodeURIComponent(tripId);
        var from = "";
        try {
            from = document.referrer ? new URL(document.referrer).pathname : "";
        } catch (error) {
            from = "";
        }
        if (from === page && window.history.length > 1) {
            window.history.back();
        } else {
            window.location.href = page;
        }
    }

    // The Export dialog of a trip (trip_export.py), once its export is known.
    function tripExportDialog(tripId, step) {
        SHELL.setState({dialog: "trip-export", id: tripId});
        SHELL.admin("/trips/" + encodeURIComponent(tripId) + "/export").then(function (info) {
            exportDialog({id: "trip:" + tripId, trip: tripId, name: info.trip.title,
                          pictures: info.trip.count, libraries: info.libraries,
                          export_info: info, video_info: {count: info.trip.videos}},
                         {step: step});
        }, function () {
            // (The message is shown already.)
            SHELL.setState({dialog: null, id: null, step: null});
        });
        return null;
    }

    function openDialog(kind, id, step) {
        if (kind === "trip-export") {
            return id ? tripExportDialog(id, step) : null;
        }
        var lib = id ? findLibrary(id) : null;
        if (kind !== "add" && kind !== "browse" && !lib) {
            return null;
        }
        SHELL.setState({dialog: kind === "browse" ? "add" : kind, id: lib ? lib.id : null,
                        path: null});
        if (kind === "add") {
            return libraryDialog(null);
        }
        if (kind === "browse") {
            // Starts at the drives: a folder named in the address is never listed.
            return libraryDialog(null, {browse: true});
        }
        if (kind === "edit") {
            return libraryDialog(lib);
        }
        if (kind === "remove") {
            return removeDialog(lib);
        }
        if (kind === "failures") {
            return failuresDialog(lib);
        }
        if (kind === "export") {
            // A library that is turned off cannot be exported (its last export can be shown).
            return lib.enabled === false && step !== "done" ? null :
                exportDialog(lib, {step: step});
        }
        return null;
    }

    /* ---------- Start ---------------------------------------------------- */

    function init() {
        setupLogFilter();
        showEmptyLog();
        byId("add-library").addEventListener("click", function () {
            openDialog("add");
        });
        byId("process-all").addEventListener("click", function () {
            queueJob("process", null, {});
        });
        function firstLoad() {
            loadState().then(function () {
                var wanted = SHELL.getState();
                if (wanted.dialog) {
                    openDialog(wanted.dialog, wanted.id, wanted.step);
                }
            }, function () {
                // Keep trying until the server answers.
                window.clearTimeout(pollTimer);
                pollTimer = window.setTimeout(firstLoad, STATE_RETRY);
            });
        }
        firstLoad();
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden && state) {
                pollJobs();
            }
        });
        // Relative times ("5 minutes ago") and folder status stay current.
        window.setInterval(function () {
            if (!document.hidden && state && !active()) {
                refreshState();
            }
        }, 60000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
