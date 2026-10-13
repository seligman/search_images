/* The Manage page's "Place names" panel: whether the place names are
   there (GeoNames, see places.py), how many pictures with a GPS position
   have their place, how many show the local time of their place (the
   towns' time zones, see local_times.py), and the buttons that download
   the place names and give the pictures their place. Both run as a
   "places" job, listed in the job panel like the others; this panel asks
   the server again every few seconds while one waits or runs.

   Pages are built with DOM calls and textContent only (never innerHTML).
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var POLL_BUSY = 3000;       // milliseconds between looks while a job waits or runs
    var box = document.getElementById("place-names");
    var timer = 0;
    var last = null;

    if (!box) {
        return;
    }

    function load() {
        window.clearTimeout(timer);
        SHELL.admin("/places", undefined, {quiet: true}).then(render, function (error) {
            clear(box);
            box.hidden = false;
            box.appendChild(el("h2", {id: "place-names-title", text: "Place names"}));
            box.appendChild(SHELL.notice("error", "The place names could not be looked at: " +
                                         ((error && error.message) || "no answer") + "."));
        });
    }

    function start(action, body, button) {
        button.disabled = true;
        SHELL.admin("/places/" + action, body || {}).then(function (data) {
            SHELL.toast(data.added === false ? "That job is already waiting or running."
                : action === "download" ? "Downloading the place names; see the job list."
                    : "Looking up the places; see the job list.", "info");
            render(data.places);
            if (!data.places.job) {
                // (A quick job can be between the queue and the list of
                // jobs done when the answer is made: look again.)
                timer = window.setTimeout(load, POLL_BUSY);
            }
        }, function () {
            button.disabled = false;
        });
    }

    function download(button) {
        SHELL.confirm({
            title: "Download the place names?",
            message: "This downloads about 10 MB from geonames.org (the towns of the world, " +
                     "their regions and countries) into the data folder, then looks up the " +
                     "place of every picture with a GPS position. Nothing about your " +
                     "pictures is sent.",
            confirmText: "Download"
        }).then(function (yes) {
            if (yes) {
                start("download", {}, button);
            }
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

    function counts(data) {
        var pictures = data.pictures || {};
        var located = pictures.located || 0;
        var text;
        if (!located) {
            text = data.gps_ignored ? "No picture has a GPS position that is used."
                : "No picture has a GPS position yet.";
        } else if (!data.available) {
            text = fmt.plural(located, "picture") + " " + (located === 1 ? "has" : "have") +
                " a GPS position.";
        } else {
            var parts = [fmt.number(pictures.named || 0) + " near a town"];
            if (pictures.region_only) {
                parts.push(fmt.number(pictures.region_only) + " farther than " + data.city_km +
                           " km from one (region and country only)");
            }
            if (pictures.far) {
                parts.push(fmt.number(pictures.far) + " with no town within " + data.max_km +
                           " km");
            }
            text = "Of " + fmt.plural(located, "picture") + " with a GPS position: " +
                parts.join(", ") + ".";
            if (pictures.pending) {
                text += " " + fmt.plural(pictures.pending, "picture") + " " +
                    (pictures.pending === 1 ? "is" : "are") + " still to be looked up.";
            }
        }
        if (data.gps_ignored) {
            // (gps_check.py: positions that cannot be where the picture was taken)
            text += " The GPS check ignored the position of " +
                fmt.plural(data.gps_ignored, "picture") + " (search is:gps-ignored).";
        }
        var estimates = data.estimates || {};
        if (estimates.estimated) {
            // (place_estimates.py: pictures without a position of their own)
            var sources = [];
            if (estimates.copy) {
                sources.push(fmt.number(estimates.copy) + " from a copy");
            }
            if (estimates.time) {
                sources.push(fmt.number(estimates.time) + " from pictures taken around the " +
                             "same time");
            }
            if (estimates.area) {
                sources.push(fmt.number(estimates.area) + " from the library's area (region " +
                             "only)");
            }
            text += " " + fmt.plural(estimates.estimated, "picture") + " without a position " +
                (estimates.estimated === 1 ? "has" : "have") + " an estimated place: " +
                sources.join(", ") + " (search is:place-estimated).";
        }
        if (estimates.owner) {
            text += " You set the place of " + fmt.plural(estimates.owner, "picture") + ".";
        }
        return text;
    }

    // Why times are what they are (local_times.status: data.local_times).
    var TIME_KINDS = [["track", "by their GPS track"], ["gps", "by their GPS time"],
                      ["gps_date", "by their GPS date"],
                      ["like", "like other pictures from the same camera"]];

    function timesText(data) {
        var times = data.local_times;
        if (!times || typeof times !== "object" || (!data.available && !data.rebuild)) {
            return "";
        }
        if (times.state === "off") {
            return "Local times are off (config.json \"local_times\"): every picture has " +
                "its camera's time.";
        }
        if (times.state === "no_zones") {
            return "The place index has no time zones yet: the next scan (or Look up every " +
                "place again) builds it again from the downloaded files, and then pictures " +
                "whose camera's clock was not the local time get the local time of their place.";
        }
        if (times.state === "no_tzdata") {
            return "Times stay as the cameras' clocks have them: Python has no time zone " +
                "database on this computer (install it with: pip install tzdata).";
        }
        var local = times.local || 0;
        if (!local) {
            return "Every picture has its camera's time: none is known to differ from the " +
                "local time of its place.";
        }
        var kinds = times.kinds || {};
        var parts = TIME_KINDS.filter(function (pair) {
            return kinds[pair[0]];
        }).map(function (pair) {
            return fmt.number(kinds[pair[0]]) + " " + pair[1];
        });
        var text = fmt.plural(local, "picture") + " " + (local === 1 ? "shows" : "show") +
            " the local time of " + (local === 1 ? "its" : "their") + " place instead of " +
            "the camera's clock" + (parts.length ? " (" + parts.join(", ") + ")" : "") + ".";
        if (times.offset_only) {
            text += " For " + fmt.plural(times.offset_only, "of them", "of them") +
                " only the UTC offset changed: the clock had the local time.";
        }
        return text + " The camera's clock is in each picture's details.";
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
        box.appendChild(el("h2", {id: "place-names-title", text: "Place names"}));
        box.appendChild(el("p", {className: "lead", text: "Pictures with a GPS position can be " +
            "found by the town, region and country they were taken in, and filtered by place. " +
            "The place names come from GeoNames, downloaded once (about 10 MB); looking up a " +
            "picture's place happens on this computer."}));
        var status = el("p", {className: "place-names-status"});
        if (data.available) {
            status.textContent = fmt.number(data.cities) + " towns known" +
                (data.built ? " (downloaded " + fmt.relative(data.built) + ")" : "") + ". " +
                counts(data);
        } else if (data.rebuild) {
            status.textContent = "Downloaded; the place index is to be built again. " +
                counts(data);
        } else {
            status.textContent = "Not downloaded yet. " + counts(data);
        }
        box.appendChild(status);
        var times = timesText(data);
        if (times) {
            box.appendChild(el("p", {className: "place-names-times", text: times}));
        }
        if (data.error) {
            // (An index of another version is no failure: it is built again.)
            box.appendChild(SHELL.notice(data.rebuild ? "" : "error", data.error));
        }
        var busy = Boolean(data.job);
        if (busy) {
            box.appendChild(el("p", {className: "place-names-job", text: jobText(data.job)}));
        } else if (data.last && data.last.state === "failed" && data.last.error) {
            box.appendChild(SHELL.notice("error", "The last try failed: " + data.last.error));
        }
        var actions = el("div", {className: "place-names-actions"});
        var get = button(data.available ? "Download again" : "Download place names",
                         data.available ? "" : "primary", download,
                         "Download the place names from geonames.org and look up every " +
                         "picture's place");
        get.disabled = busy;
        actions.appendChild(get);
        var pending = data.pictures ? data.pictures.pending || 0 : 0;
        if (data.rebuild && !data.available) {
            // (The place index is from another version: the job builds it
            // again from the files downloaded before.)
            var rebuild = button("Add place names", "primary", function (node) {
                start("label", {}, node);
            }, "Build the place index again from the downloaded files and look up the places");
            rebuild.disabled = busy;
            actions.appendChild(rebuild);
        } else if (data.available && pending) {
            var add = button("Add place names (" + fmt.number(pending) + ")", "primary",
                             function (node) {
                                 start("label", {}, node);
                             }, "Look up the place of the pictures that have none yet");
            add.disabled = busy;
            actions.appendChild(add);
        }
        if (data.available && data.pictures && data.pictures.located) {
            var again = button("Look up every place again", "subtle", function (node) {
                start("label", {redo: true}, node);
            }, "Look up the place of every picture with a GPS position again");
            again.disabled = busy;
            actions.appendChild(again);
        }
        box.appendChild(actions);
        var credit = el("p", {className: "place-names-credit"}, [
            "Place names: ",
            el("a", {href: data.attribution_url, text: "GeoNames", target: "_blank",
                     rel: "noopener"}),
            ", ",
            el("a", {href: data.license_url, text: "CC BY 4.0", target: "_blank",
                     rel: "noopener license"})
        ]);
        box.appendChild(credit);
        if (busy) {
            timer = window.setTimeout(load, POLL_BUSY);
        }
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && last) {
            load();
        }
    });
    load();
})();
