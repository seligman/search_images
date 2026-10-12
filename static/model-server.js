/* Settings page: the "Model server" panel (local_server in config.json).

   Shows whether the local model server runs, lets you start and stop it,
   and change the settings the server allows from a page: the model and
   projector (only files found in its models folder), slots, context per
   slot, port, the daily stop time and the auto start/stop switches. The
   program and its command line can only be set in config.json; they are
   shown read-only.

   Fires a "model-server-changed" event on the document when a server
   starts or stops, so the endpoint list can look at its status again.
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var field = SHELL.field;
    var notice = SHELL.notice;

    var POLL_BUSY = 2000;       // while a server starts or stops
    var POLL_IDLE = 15000;
    var BUSY = ["starting", "loading", "stopping"];
    var LABELS = {
        "not configured": "Not set up",
        off: "Turned off",
        stopped: "Stopped",
        starting: "Starting",
        loading: "Loading its model",
        ready: "Ready",
        stopping: "Stopping",
        error: "Error",
        external: "Running (not started here)"
    };
    var CLASSES = {ready: "ok", external: "ok", starting: "warn", loading: "warn",
                   stopping: "warn", error: "bad"};

    var state = null;           // the last /model-server reply
    var cards = {};             // endpoint name -> {node, form, update(server)}
    var rendered = null;        // the server names the panel shows
    var timer = null;
    var request = null;

    function byId(id) {
        return document.getElementById(id);
    }

    function button(text, className, onClick, title) {
        var node = el("button", {type: "button", className: "button " + (className || ""),
                                 text: text, title: title});
        if (onClick) {
            node.addEventListener("click", onClick);
        }
        return node;
    }

    function busy(server) {
        return BUSY.indexOf(server.state) >= 0;
    }

    function changed() {
        var event;
        try {
            event = new CustomEvent("model-server-changed");
        } catch (error) {
            event = document.createEvent("Event");
            event.initEvent("model-server-changed", false, false);
        }
        document.dispatchEvent(event);
    }

    /* ---------- Loading -------------------------------------------------- */

    function schedule() {
        window.clearTimeout(timer);
        var waiting = state && state.servers.some(busy);
        timer = window.setTimeout(function () {
            if (!document.hidden) {
                load();
            } else {
                schedule();
            }
        }, waiting ? POLL_BUSY : POLL_IDLE);
    }

    function load() {
        if (request) {
            return request;
        }
        var before = state ? JSON.stringify(state.servers.map(function (server) {
            return [server.endpoint, server.state, server.owned];
        })) : null;
        request = SHELL.admin("/model-server", undefined, {quiet: true}).then(function (data) {
            request = null;
            state = data;
            render();
            var after = JSON.stringify(data.servers.map(function (server) {
                return [server.endpoint, server.state, server.owned];
            }));
            if (before !== null && before !== after) {
                changed();
            }
            schedule();
            return data;
        }, function (error) {
            request = null;
            var holder = byId("model-server");
            if (!state && holder) {
                clear(holder);
                holder.appendChild(el("p", {className: "message", text: "The model server's " +
                    "state could not be loaded: " + error.message}));
            }
            schedule();
        });
        return request;
    }

    /* ---------- Rendering ------------------------------------------------ */

    function render() {
        var holder = byId("model-server");
        var names = state.servers.map(function (server) {
            return server.endpoint;
        }).join("\n");
        if (names !== rendered) {
            rendered = names;
            cards = {};
            clear(holder);
            if (!state.servers.length) {
                holder.appendChild(setupHelp());
                return;
            }
            state.servers.forEach(function (server) {
                var card = serverCard(server);
                cards[server.endpoint] = card;
                holder.appendChild(card.node);
            });
        }
        state.servers.forEach(function (server) {
            if (cards[server.endpoint]) {
                cards[server.endpoint].update(server);
            }
        });
    }

    function setupHelp() {
        var setup = state.setup || {};
        var paths = "Put your own paths in binary and models_dir" + (setup.windows ?
            " (in JSON each backslash is doubled)" : "") + "; \u201cmodel\u201d is a file in " +
            "the models folder.";
        return el("div", {className: "empty server-setup"}, [
            el("h2", {text: "Start and stop the model server from here"}),
            el("p", {text: "This page can start llama.cpp's llama-server for a local endpoint " +
                "and stop it again, and describing can start it by itself when it is needed. " +
                "The program and the models folder can only be set in config.json: add a " +
                "\u201clocal_server\u201d section next to \u201cendpoints\u201d (with a comma " +
                "after the entry before it), then reload this page."}),
            setup.config_path ? el("p", {className: "muted", text: "config.json is " +
                setup.config_path + "."}) : null,
            el("pre", {className: "server-snippet", text: setup.snippet || ""}),
            el("p", {className: "muted", text: setup.endpoint ?
                "The example serves the endpoint " + setup.endpoint + " on port " + setup.port +
                ". " + paths + " See SETTINGS.md for every setting." :
                "First add a llama.cpp endpoint (above) with the base URL " +
                "http://127.0.0.1:" + (setup.port || 8091) + "/v1, and name it in " +
                "\u201cendpoint\u201d. " + paths + " See SETTINGS.md for every setting."})
        ]);
    }

    function option(value, text) {
        return el("option", {value: value, text: text});
    }

    function serverCard(server) {
        var pill = el("span", {className: "pill"});
        var head = el("div", {className: "card-head"}, [
            el("h2", {text: server.endpoint}), pill
        ]);
        var message = el("p", {className: "card-note"});
        var facts = el("dl", {className: "server-facts"});
        var startButton = button("Start", "primary", function () {
            start(server.endpoint);
        });
        var stopButton = button("Stop", "", function () {
            stop(server.endpoint);
        });
        var saveButton = button("Save settings", "", null, "Save the settings above to " +
                                "config.json");
        saveButton.disabled = true;
        var actions = el("div", {className: "card-actions"}, [
            startButton, stopButton, el("span", {className: "spacer"}), saveButton
        ]);
        var formErrors = el("p", {className: "field-error", hidden: true});
        var inputs = {};
        var fields = {};

        // The settings form.
        inputs.model = el("select");
        fields.model = field("Model", inputs.model, {hint: "A model file in the models " +
                                                            "folder."});
        inputs.mmproj = el("select");
        fields.mmproj = field("Vision projector", inputs.mmproj, {hint: "The mmproj file " +
            "that lets the model see pictures."});
        inputs.slots = el("input", {type: "number", className: "input", min: 1, max: 64,
                                    step: 1, inputMode: "numeric"});
        fields.slots = field("Slots", inputs.slots, {hint: "Pictures described at the same " +
                                                            "time."});
        inputs.ctx_per_slot = el("input", {type: "number", className: "input", min: 512,
                                           step: 512, inputMode: "numeric"});
        fields.ctx_per_slot = field("Context per slot (tokens)", inputs.ctx_per_slot,
                                    {hint: "About 8192 suits most pictures."});
        inputs.port = el("input", {type: "number", className: "input", min: 1024, max: 65535,
                                   step: 1, inputMode: "numeric"});
        fields.port = field("Port", inputs.port, {hint: "On this computer (127.0.0.1); the " +
                                                          "endpoint's base URL uses it."});
        inputs.stop_at = el("input", {type: "time", className: "input"});
        fields.stop_at = field("Stop every day at", inputs.stop_at, {optional: true,
            hint: "Jobs using it are stopped 10 minutes before (resume them later), and it " +
                "is not started for jobs until an hour after."});
        inputs.auto_start = el("input", {type: "checkbox"});
        inputs.auto_stop = el("input", {type: "checkbox"});
        var checks = el("div", {className: "server-checks"}, [
            el("label", {className: "check"}, [inputs.auto_start, el("span", null, [
                "Start it when describing needs it",
                el("span", {className: "field-hint", text: "Describe and refine jobs that " +
                    "use this endpoint start the server, and wait for it to load."})])]),
            el("label", {className: "check"}, [inputs.auto_stop, el("span", null, [
                "Stop it when such a job has finished",
                el("span", {className: "field-hint", text: "Only a server the job started " +
                    "itself; one you started keeps running."})])])
        ]);
        var form = el("div", {className: "server-form"}, [
            el("div", {className: "field-grid"}, [fields.model.node, fields.mmproj.node]),
            el("div", {className: "field-grid"}, [fields.slots.node, fields.ctx_per_slot.node,
                                                  fields.port.node, fields.stop_at.node]),
            checks, formErrors
        ]);

        var command = el("pre", {className: "server-command"});
        var program = el("dl", {className: "server-facts"});
        var config = el("details", {className: "more"}, [
            el("summary", {text: "Program and command line (set in config.json)"}),
            program, command
        ]);
        var log = el("pre", {className: "server-log", tabIndex: 0,
                             attrs: {"aria-label": "Model server log"}});
        var logBox = el("details", {className: "more"}, [
            el("summary", {text: "Log (the last lines)"}), log
        ]);
        var node = el("article", {className: "card server-card", id: "server-" +
                                  server.endpoint.replace(/[^A-Za-z0-9_-]/g, "-")},
                      [head, message, facts, form, actions, config, logBox]);

        var current = null;     // the settings the form started from
        var dirty = false;
        var filledFrom = null;  // what the form was last filled from

        function values() {
            return {
                model: inputs.model.value,
                mmproj: inputs.mmproj.value,
                slots: inputs.slots.value.trim(),
                ctx_per_slot: inputs.ctx_per_slot.value.trim(),
                port: inputs.port.value.trim(),
                stop_at: inputs.stop_at.value,
                auto_start: inputs.auto_start.checked,
                auto_stop: inputs.auto_stop.checked
            };
        }

        function stored(server) {
            return {
                model: server.model || "",
                mmproj: server.mmproj || "",
                slots: String(server.slots),
                ctx_per_slot: String(server.ctx_per_slot),
                port: String(server.port),
                stop_at: server.stop_at || "",
                auto_start: Boolean(server.auto_start),
                auto_stop: Boolean(server.auto_stop)
            };
        }

        function differences() {
            var now = values();
            var result = {};
            Object.keys(now).forEach(function (key) {
                if (now[key] !== current[key]) {
                    result[key] = now[key];
                }
            });
            return result;
        }

        function fillChoices(server) {
            var choices = server.choices || {};
            var models = (choices.models || []).slice();
            if (server.model && models.indexOf(server.model) < 0) {
                models.unshift(server.model);
            }
            clear(inputs.model);
            if (!models.length) {
                inputs.model.appendChild(option("", choices.checking ? "Looking..." :
                    "No model files found"));
            }
            models.forEach(function (name) {
                inputs.model.appendChild(option(name, name));
            });
            clear(inputs.mmproj);
            inputs.mmproj.appendChild(option("", server.mmproj_auto ?
                "Automatic (" + server.mmproj_auto + ")" : "Automatic (none found)"));
            var projectors = (choices.mmprojs || []).slice();
            if (server.mmproj && projectors.indexOf(server.mmproj) < 0) {
                projectors.unshift(server.mmproj);
            }
            projectors.forEach(function (name) {
                inputs.mmproj.appendChild(option(name, name));
            });
            if (choices.error) {
                fields.model.error.textContent = choices.error;
                fields.model.error.hidden = false;
            } else {
                fields.model.error.hidden = true;
            }
        }

        function fillForm(server) {
            fillChoices(server);
            current = stored(server);
            inputs.model.value = current.model;
            inputs.mmproj.value = current.mmproj;
            inputs.slots.value = current.slots;
            inputs.ctx_per_slot.value = current.ctx_per_slot;
            inputs.port.value = current.port;
            inputs.stop_at.value = current.stop_at;
            inputs.auto_start.checked = current.auto_start;
            inputs.auto_stop.checked = current.auto_stop;
            dirty = false;
            saveButton.disabled = true;
        }

        function factRow(list, term, value) {
            if (value === null || value === undefined || value === "") {
                return;
            }
            list.appendChild(el("dt", {text: term}));
            list.appendChild(el("dd", null, value));
        }

        function update(server) {
            var label = LABELS[server.state] || server.state;
            pill.className = "pill " + (CLASSES[server.state] || "");
            pill.textContent = label;
            message.textContent = server.problem && !server.owned && server.state !== "external" ?
                server.problem : server.message || (server.checking && !server.owned ?
                    "Looking at the model files..." : "");
            message.className = "card-note" + (server.state === "error" || (server.problem &&
                !server.owned) ? " card-problem" : "");
            clear(facts);
            factRow(facts, "Endpoint", server.endpoint_exists ? server.endpoint + " (" +
                    server.endpoint_url + ")" : server.endpoint + " (not set up yet)");
            var served = server.served || {};
            var loaded = [];
            if (served.model) {
                loaded.push(served.model);
            }
            if (served.slots) {
                loaded.push(fmt.plural(served.slots, "slot"));
            }
            if (served.vision === true) {
                loaded.push("sees pictures");
            } else if (served.vision === false) {
                loaded.push("cannot see pictures");
            }
            factRow(facts, "Loaded", loaded.join(" \u00b7 "));
            if (server.owned) {
                factRow(facts, "Started", fmt.relative(server.started) + " by " +
                        (server.started_by || "this page") + (server.pid ? " (process " +
                        server.pid + ")" : ""));
            }
            if (server.in_use_by && server.in_use_by.length) {
                factRow(facts, "In use by", server.in_use_by.join(", "));
            }
            if (server.stop_window) {
                factRow(facts, "Now", server.stop_window);
            }
            startButton.disabled = busy(server) || server.owned ||
                server.state === "external" || Boolean(server.problem) ||
                Boolean(server.checking);
            startButton.title = server.problem || (server.checking ?
                "Looking at the model files (the models folder may be on a share)" : "");
            stopButton.disabled = !server.owned || server.state === "stopping";
            stopButton.title = server.owned ? "" : server.state === "external" ?
                "It was not started here, so it is not stopped here either" : "";
            clear(program);
            factRow(program, "Program", server.launch_command && server.launch_command.length ?
                    "A command of your own (launch_command)" : server.binary || "(not set)");
            factRow(program, "Models folder", server.models_dir || "(not set)");
            factRow(program, "Starts within", fmt.duration(server.start_timeout));
            command.textContent = server.command ? "$ " + server.command : "";
            var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
            log.textContent = (server.log_tail || []).join("\n") || "(empty)";
            if (atBottom) {
                log.scrollTop = log.scrollHeight;
            }
            var key = JSON.stringify([stored(server), server.choices || null,
                                      server.mmproj_auto || ""]);
            if (!dirty && key !== filledFrom) {
                filledFrom = key;
                fillForm(server);
            }
        }

        function clearErrors() {
            formErrors.hidden = true;
            Object.keys(fields).forEach(function (key) {
                fields[key].error.hidden = true;
                fields[key].control.removeAttribute("aria-invalid");
            });
        }

        function save() {
            clearErrors();
            var changes = differences();
            if (!Object.keys(changes).length) {
                return;
            }
            saveButton.disabled = true;
            SHELL.admin("/model-server/settings", {endpoint: server.endpoint,
                                                   settings: changes}, {quiet: true})
                .then(function (reply) {
                    dirty = false;
                    SHELL.toast("Saved the model server settings.", "ok", 4);
                    (reply.notes || []).forEach(function (text) {
                        SHELL.toast(text, "info", 6);
                    });
                    filledFrom = null;
                    update(reply.server);
                    changed();
                }, function (error) {
                    saveButton.disabled = false;
                    var shown = false;
                    Object.keys(error.fields || {}).forEach(function (key) {
                        if (fields[key]) {
                            fields[key].error.textContent = error.fields[key];
                            fields[key].error.hidden = false;
                            fields[key].control.setAttribute("aria-invalid", "true");
                            shown = true;
                        }
                    });
                    if (!shown && error.status !== 409) {
                        formErrors.textContent = error.message;
                        formErrors.hidden = false;
                    } else if (error.status === 409 && !error.data.reload) {
                        formErrors.textContent = error.message;
                        formErrors.hidden = false;
                    }
                });
        }

        Object.keys(inputs).forEach(function (key) {
            var input = inputs[key];
            input.addEventListener(input.tagName === "SELECT" || input.type === "checkbox" ?
                "change" : "input", function () {
                    dirty = Object.keys(differences()).length > 0;
                    saveButton.disabled = !dirty;
                });
        });
        saveButton.addEventListener("click", save);
        return {node: node, update: update};
    }

    /* ---------- Actions -------------------------------------------------- */

    function findServer(name) {
        var found = null;
        (state ? state.servers : []).forEach(function (server) {
            if (server.endpoint === name) {
                found = server;
            }
        });
        return found;
    }

    function apply(reply) {
        if (reply && reply.server && state) {
            state.servers = state.servers.map(function (server) {
                return server.endpoint === reply.server.endpoint ? reply.server : server;
            });
            render();
        }
        changed();
        schedule();
    }

    function start(name) {
        SHELL.admin("/model-server/start", {endpoint: name}).then(function (reply) {
            SHELL.toast(reply.message || "Starting the model server.", "info", 4);
            apply(reply);
        }, function () {
            load();
        });
    }

    function stop(name) {
        var server = findServer(name) || {};
        var ask = Promise.resolve(true);
        if (server.in_use_by && server.in_use_by.length) {
            ask = SHELL.confirm({
                title: "Stop the model server?",
                message: "It is in use by " + server.in_use_by.join(", ") + ". That job " +
                    "cannot describe any more pictures while the server is stopped.",
                confirmText: "Stop the server",
                danger: true
            });
        }
        ask.then(function (yes) {
            if (!yes) {
                return;
            }
            SHELL.admin("/model-server/stop", {endpoint: name}).then(function (reply) {
                SHELL.toast(reply.message || "Stopping the model server.", "info", 4);
                apply(reply);
            }, function () {
                load();
            });
        });
    }

    function init() {
        if (!byId("model-server")) {
            return;
        }
        load();
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) {
                load();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
