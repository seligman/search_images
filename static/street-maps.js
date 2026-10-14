/* The Manage page's "Street maps" panel: whether the street maps
   (street_maps.py: OpenStreetMap tiles down to street level around the
   places of the pictures and along the drive routes) are there, how large,
   and how many places of pictures they leave out; the last size check; and
   the buttons that check the size of a download (first), download (after
   a confirm that names the size; one of 5 GB or more only after a second,
   explicit one) and delete them. The size check and the download run as a
   "maps" job, listed in the job panel like the others; this panel asks the
   server again every few seconds while one waits or runs. A download that
   stopped half way is named (its build, how far it got) while no job
   runs.

   Pages are built with DOM calls and textContent only (never innerHTML).
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var POLL_BUSY = 3000;       // milliseconds between looks while a job waits or runs
    var box = document.getElementById("street-maps");
    var timer = 0;
    var last = null;
    var full = true;            // (the first look also counts the places left out)

    if (!box) {
        return;
    }

    // "1.4 GB", "551 MB": decimal, as the sizes of downloads are given.
    function size(value) {
        var n = Number(value) || 0;
        var units = [["GB", 1e9], ["MB", 1e6], ["kB", 1e3]];
        for (var k = 0; k < units.length; k++) {
            if (n >= units[k][1]) {
                var shown = n / units[k][1];
                return (shown < 10 ? shown.toFixed(1) : Math.round(shown)) + " " + units[k][0];
            }
        }
        return fmt.plural(Math.round(n), "byte");
    }

    function load() {
        window.clearTimeout(timer);
        SHELL.admin("/maps" + (full ? "?full=1" : ""), undefined, {quiet: true})
            .then(render, function (error) {
                clear(box);
                box.hidden = false;
                box.appendChild(el("h2", {id: "street-maps-title", text: "Street maps"}));
                box.appendChild(SHELL.notice("error", "The street maps could not be looked at: " +
                                             ((error && error.message) || "no answer") + "."));
            });
        full = false;
    }

    function start(action, body, button, words) {
        button.disabled = true;
        SHELL.admin("/maps/" + action, body || {}).then(function (data) {
            if (action === "remove") {
                SHELL.toast(data.removed ? "The street maps were deleted." :
                            "There were no street maps.", "info");
                render(data.maps);
                return;
            }
            SHELL.toast(data.added === false ? "That job is already waiting or running."
                : words, "info");
            render(data.maps);
            if (!data.maps.job) {
                timer = window.setTimeout(load, POLL_BUSY);
            }
        }, function () {
            button.disabled = false;
        });
    }

    function button(text, className, onClick, title) {
        var node = el("button", {type: "button", className: "button " + (className || ""),
                                 text: text, title: title || ""});
        node.addEventListener("click", function () {
            onClick(node);
        });
        return node;
    }

    // What the map's host learns of a download (whole_degrees or not).
    function privacyText(plan) {
        if (plan.mode === "whole_degrees") {
            return "Every tile of " + fmt.plural(plan.squares, "whole-degree square") +
                " is asked for and only those around the places are kept, so the map's " +
                "host learns no more than whole degrees of where your pictures are.";
        }
        return "Only the tiles kept are asked for (\"whole_degrees\" is off in " +
            "config.json), so the map's host can see which areas are downloaded: " +
            "where your pictures are.";
    }

    function planText(plan) {
        return "Checked " + fmt.relative(plan.at) + ": download " +
            size(plan.download_bytes) + " to keep " + size(plan.keep_bytes) + " (" +
            fmt.plural(plan.tiles, "tile") + ", zoom 0 to " + plan.max_zoom + "), the area " +
            plan.radius_km + " km around " + fmt.plural(plan.spots, "place") +
            " of pictures" + (plan.route_points ? " and " + plan.route_km +
                              " km either side of the drive routes" : "") + ". " +
            privacyText(plan);
    }

    // What becomes of a download that stopped half way, for the confirm ("" none).
    function stoppedText(stopped, plan) {
        if (!stopped) {
            return "";
        }
        return stopped.build && stopped.build === plan.build
            ? " It goes on where the download that stopped half way stopped."
            : " The download that stopped half way (build " + (stopped.build || "?") +
              ") is not of this size check: it starts over.";
    }

    function download(node, plan, stopped) {
        var message = "This downloads " + size(plan.download_bytes) + " of OpenStreetMap " +
            "map tiles (build " + plan.build + ") into the data folder and keeps " +
            size(plan.keep_bytes) + " of them. " + privacyText(plan) +
            stoppedText(stopped, plan) + " A download that stops goes on where it stopped " +
            "when started again.";
        SHELL.confirm({
            title: "Download the street maps?",
            message: message,
            confirmText: "Download " + size(plan.download_bytes)
        }).then(function (yes) {
            if (!yes) {
                return;
            }
            if (!plan.large) {
                start("download", {expect_bytes: plan.download_bytes, source: plan.source},
                      node, "Downloading the street maps; see the job list.");
                return;
            }
            // 5 GB or more: only when asked for again, explicitly.
            SHELL.confirm({
                title: "Download " + size(Math.max(plan.download_bytes, plan.keep_bytes)) +
                       " of street maps?",
                message: "That is 5 GB or more (" + size(plan.download_bytes) + " to download, " +
                         size(plan.keep_bytes) + " to keep): more than street maps are worth, " +
                         "as a rule. A smaller \"radius_km\", or \"max_zoom\": 14, in " +
                         "config.json \"street_maps\" makes it much smaller (then check the " +
                         "size again). Download all of it anyway?",
                confirmText: "Download " + size(plan.download_bytes) + " anyway",
                danger: true
            }).then(function (sure) {
                if (sure) {
                    start("download", {expect_bytes: plan.download_bytes, source: plan.source,
                                       allow_large: true},
                          node, "Downloading the street maps; see the job list.");
                }
            });
        });
    }

    function remove(node) {
        SHELL.confirm({
            title: "Delete the street maps?",
            message: "The maps show the world image only again, until the street maps are " +
                     "downloaded again. Exports made with them keep theirs.",
            confirmText: "Delete",
            danger: true
        }).then(function (yes) {
            if (yes) {
                start("remove", {}, node);
            }
        });
    }

    function jobText(job) {
        var text = SHELL.jobs.title(job) + ": " + SHELL.jobs.state(job.state).toLowerCase();
        if (job.state !== "queued") {
            text += " \u00b7 " + SHELL.jobs.stage(job.stage, job);
            var progress = SHELL.jobs.progress(job);
            if (progress) {
                text += " \u00b7 " + progress;
            }
        }
        return text;
    }

    function render(data) {
        window.clearTimeout(timer);
        last = data;
        clear(box);
        box.hidden = false;
        box.appendChild(el("h2", {id: "street-maps-title", text: "Street maps"}));
        box.appendChild(el("p", {className: "lead", text: "Maps down to street level, of the " +
            "places of your pictures and along the drive routes, for the Map page, the trips' " +
            "maps and the viewer's map of a drive. They are OpenStreetMap data, downloaded " +
            "once into the data folder; the pages (and exports that include them) never " +
            "ask any other site for anything."}));
        var manifest = data.manifest || {};
        var status = el("p", {className: "street-maps-status"});
        if (data.available) {
            status.textContent = "Downloaded" + (manifest.downloaded ? " " +
                fmt.relative(manifest.downloaded) : "") + (manifest.build ? " (build " +
                manifest.build + ")" : "") + ": " + size(data.bytes) + ", " +
                fmt.plural(data.tiles || 0, "tile") + ", zoom " + data.min_zoom + " to " +
                data.max_zoom + ".";
            if (data.outside) {
                status.textContent += " " + fmt.plural(data.outside, "place") + " of " +
                    "pictures " + (data.outside === 1 ? "has" : "have") + " no street map " +
                    "yet: check the size and download again to add " +
                    (data.outside === 1 ? "it." : "them.");
            }
        } else {
            status.textContent = "Not downloaded yet: the maps show the world image only.";
        }
        box.appendChild(status);
        if (data.error) {
            box.appendChild(SHELL.notice("error", data.error));
        }
        var busy = Boolean(data.job);
        var stopped = data.downloading || null;
        if (stopped && !busy) {
            // (While a job runs, the download it holds is not one that stopped.)
            box.appendChild(SHELL.notice("warn", "A download stopped half way (build " +
                (stopped.build || "?") + ", " + (stopped.done || 0) + " of " +
                (stopped.total || "?") + " parts): Download goes on where it stopped while " +
                "that build is there."));
        }
        var plan = data.plan;
        if (plan) {
            box.appendChild(el("p", {className: "street-maps-plan", text: planText(plan)}));
            if (plan.large) {
                box.appendChild(SHELL.notice("warn", "That is 5 GB or more: it is only " +
                    "downloaded when you allow it a second time. A smaller \"radius_km\" or " +
                    "\"max_zoom\": 14 in config.json \"street_maps\" makes it much smaller."));
            }
        }
        if (busy) {
            box.appendChild(el("p", {className: "street-maps-job", text: jobText(data.job)}));
        } else if (data.last && data.last.state === "failed" && data.last.error) {
            box.appendChild(SHELL.notice("error", "The last try failed: " + data.last.error));
        }
        var actions = el("div", {className: "place-names-actions street-maps-actions"});
        var check = button(plan ? "Check the size again" : "Check the size", plan ? ""
            : "primary", function (node) {
                start("plan", {}, node, "Checking the size of the street maps (it reads " +
                      "the map's table of contents, a few MB); see the job list.");
            }, "Work out what a download would be, before anything is downloaded");
        check.disabled = busy;
        actions.appendChild(check);
        if (plan) {
            var get = button("Download (" + size(plan.download_bytes) + ")",
                             data.available || plan.large ? "" : "primary", function (node) {
                                 download(node, plan, stopped);
                             }, "Download the street maps the last size check worked out");
            get.disabled = busy;
            actions.appendChild(get);
        }
        if (data.available) {
            var drop = button("Delete street maps", "subtle", remove,
                              "Delete the downloaded street maps");
            drop.disabled = busy;
            actions.appendChild(drop);
        }
        box.appendChild(actions);
        var settings = data.settings || {};
        box.appendChild(el("p", {className: "field-hint", text: "What is downloaded: " +
            (settings.radius_km || 20) + " km around each place of a picture, " +
            (settings.route_km === undefined ? 5 : settings.route_km) + " km either side " +
            "of the drive routes, zoom 0 to " + (settings.max_zoom || 15) + " (config.json " +
            "\"street_maps\"). From the command line: " +
            (data.command || "python street_maps.py --download") + "."}));
        box.appendChild(el("p", {className: "place-names-credit",
                                 text: "Map data " + (data.attribution || "") + ", " +
                                     (data.license || "ODbL") + "; the tiles are the " +
                                     "Protomaps basemap."}));
        var wasBusy = box.getAttribute("data-busy") === "1";
        box.setAttribute("data-busy", busy ? "1" : "0");
        if (busy) {
            timer = window.setTimeout(load, POLL_BUSY);
        } else if (wasBusy) {
            // (A job has just ended: look once more, counting the places left out.)
            full = true;
            timer = window.setTimeout(load, 0);
        }
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && last) {
            load();
        }
    });
    load();
})();
