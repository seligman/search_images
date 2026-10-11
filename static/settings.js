/* Settings page: the endpoints (LLM servers and cloud services) that
   describe pictures. Add, edit, remove, make default, list models and
   Test (one real describe of the bundled sample picture).

   API keys are write-only: the page only ever learns where a key comes
   from (key_source) and whether one is set, never the key itself.

   ?dialog=add opens the Add dialog; ?dialog=edit|test&name=<endpoint>
   open those, so they can be linked to. A Test opened from the address
   always asks first: an address alone never makes the server send a
   picture anywhere.
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;

    var TEST_COST_KEY = "searchImages.testCost.";
    var STATUS_REFRESH = 30000;     // how often the model servers' status is checked
    var LOCAL_ORDER = ["llamacpp", "lmstudio", "ollama", "ollama-openai", "vllm", "local"];
    var CLOUD_ORDER = ["openai", "anthropic", "gemini", "openrouter", "azure"];
    var ERROR_KINDS = {
        retryable: "Temporary problem",
        fatal: "Refused (check the API key, credit or billing)",
        endpoint: "The endpoint did not work",
        image: "The picture was refused",
        content: "The answer could not be used",
        timeout: "No answer in time",
        local: "Problem on this computer"
    };
    // Fields of a test answer, in reading order, with plain labels.
    var PARSED_FIELDS = [
        ["title", "Title"], ["description", "Description"], ["subjects", "Subjects"],
        ["tags", "Tags"], ["text", "Text in the picture"], ["text_language", "Text language"],
        ["photo_type", "Kind of picture"], ["document_type", "Document type"],
        ["setting", "Setting"], ["scene", "Scene"], ["time_of_day", "Time of day"],
        ["event", "Event"], ["people", "People"], ["people_descriptors", "People seen"],
        ["animals", "Animals"], ["food", "Food"], ["vehicles", "Vehicles"],
        ["colors", "Colours"], ["quality_flags", "Quality notes"], ["sensitive", "Sensitive"],
        ["private_info", "Private information"], ["aesthetic", "Looks (1-5)"],
        ["sharpness", "Sharpness (1-5)"], ["exposure", "Exposure (1-5)"]
    ];

    var state = null;           // the last /endpoints reply
    var statusRetries = 0;
    var request = null;
    var rendered = null;        // what the list shows, to redraw it only when it changes

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

    var fieldCount = 0;

    // A labelled field. The label (and the hint) are tied to the control
    // by id, also when the field holds more than the control (options.div).
    function field(label, control, options) {
        options = options || {};
        fieldCount += 1;
        var labelId = "settings-field-" + fieldCount;
        var error = el("span", {className: "field-error", hidden: true,
                                attrs: {"aria-live": "polite"}});
        var labelNode = el("span", {className: "field-label", id: labelId}, [label]);
        if (options.optional) {
            labelNode.appendChild(el("span", {className: "optional", text: " (optional)"}));
        }
        var hint = options.hint ? el("span", {className: "field-hint", id: labelId + "-hint"},
                                     options.hint) : null;
        var node = el(options.div ? "div" : "label", {className: "field"}, [
            labelNode, control, hint, options.after || null, error
        ]);
        var target = options.control || control;
        target.setAttribute("aria-labelledby", labelId);
        if (hint) {
            target.setAttribute("aria-describedby", labelId + "-hint");
        }
        return {node: node, error: error, control: target};
    }

    function notice(kind, content, actions) {
        var box = el("div", {className: "notice " + (kind || "")});
        box.appendChild(SHELL.append(el("div", {className: "notice-text"}), content));
        (actions || []).forEach(function (action) {
            box.appendChild(action);
        });
        return box;
    }

    function showNotice(kind, content) {
        var holder = byId("settings-notices");
        var close = button("Dismiss", "small");
        var box = notice(kind, content, [close]);
        close.addEventListener("click", function () {
            holder.removeChild(box);
        });
        holder.appendChild(box);
    }

    function warningList(warnings) {
        if (warnings.length === 1) {
            return warnings[0];
        }
        return el("ul", {className: "warning-list"}, warnings.map(function (text) {
            return el("li", {text: text});
        }));
    }

    /* ---------- Loading -------------------------------------------------- */

    function load() {
        if (request) {
            return request;
        }
        request = SHELL.admin("/endpoints", undefined, {quiet: Boolean(state)}).then(function (data) {
            request = null;
            state = data;
            var key = JSON.stringify([data.endpoints, data.default_endpoint]);
            if (key !== rendered) {
                rendered = key;
                render();
            }
            var checking = data.endpoints.some(function (endpoint) {
                return endpoint.status && endpoint.status.state === "checking";
            });
            if (checking && statusRetries < 10) {
                statusRetries += 1;
                window.setTimeout(load, 2000);
            } else if (!checking) {
                statusRetries = 0;
            }
            return data;
        }, function (error) {
            request = null;
            if (!state) {
                byId("settings-status").textContent = "The settings could not be loaded: " +
                    error.message;
            }
            throw error;
        });
        return request;
    }

    function reload() {
        load().catch(function () {
            // Shown already.
        });
    }

    function findEndpoint(name) {
        var found = null;
        (state ? state.endpoints : []).forEach(function (endpoint) {
            if (endpoint.name === name) {
                found = endpoint;
            }
        });
        return found;
    }

    /* ---------- The list ------------------------------------------------- */

    function keyText(endpoint) {
        var source = endpoint.key_source || "none";
        if (source === "none") {
            return endpoint.local ? "None needed" : "None set";
        }
        if (source === "config") {
            return "Saved in config.json";
        }
        if (source === "file") {
            return "From a key file";
        }
        if (source === "file missing") {
            return "Key file missing";
        }
        if (source === "file empty") {
            return "Key file is empty";
        }
        if (source === "file checking") {
            return "Reading the key file...";
        }
        if (source === "file not responding") {
            return "Key file does not answer (offline share?)";
        }
        if (source.indexOf("env ") === 0) {
            var rest = source.slice(4);
            if (/ not set$/.test(rest)) {
                return "Environment variable " + rest.slice(0, -8) + " is not set";
            }
            return "Environment variable " + rest;
        }
        return source;
    }

    function keyProblem(endpoint) {
        var source = endpoint.key_source || "none";
        return !endpoint.key_set && (!endpoint.local || source !== "none");
    }

    function statusCell(endpoint) {
        if (endpoint.enabled === false) {
            return [el("span", {className: "pill", text: "Turned off"})];
        }
        if (!endpoint.local) {
            return [
                el("span", {className: "pill warn plain", text: "Cloud service"}),
                el("div", {className: "cell-sub", text: "Sends pictures to " + endpoint.host +
                                                        "; its API terms apply."})
            ];
        }
        var status = endpoint.status || {state: "checking"};
        var classes = {ready: "ok", loading: "warn", "not running": "bad"};
        var labels = {ready: "Ready", loading: "Loading its model", "not running": "Not running",
                      checking: "Checking...", unknown: "Unknown"};
        var nodes = [el("span", {className: "pill " + (classes[status.state] || ""),
                                 text: labels[status.state] || status.state})];
        var details = [];
        if (status.model) {
            details.push("Loaded: " + status.model);
        }
        if (status.slots) {
            details.push(fmt.plural(status.slots, "slot"));
        }
        if (details.length) {
            nodes.push(el("div", {className: "cell-sub", text: details.join(" \u00b7 ")}));
        }
        if (status.hint) {
            nodes.push(el("div", {className: "cell-sub", text: status.hint}));
        }
        return nodes;
    }

    function usedByText(endpoint) {
        var names = {};
        (SITE.libraryList || []).forEach(function (item) {
            names[item.id] = item.name;
        });
        var used = endpoint.used_by || [];
        if (!used.length) {
            return endpoint.default ? "Libraries without an endpoint of their own" : "No library";
        }
        var known = used.filter(function (id) {
            return names[id];
        }).map(function (id) {
            return names[id];
        });
        var others = used.length - known.length;
        if (others) {
            known.push(fmt.plural(others, "library", "libraries") + " that " +
                       (others === 1 ? "is" : "are") + " turned off");
        }
        return known.join(", ");
    }

    // The empty endpoint a new config.json starts with (no model, no
    // address, no key): nothing has been set up yet.
    function placeholder(endpoint) {
        var stored = endpoint.stored || {};
        return !endpoint.model && !stored.base_url && !stored.preset && !stored.api_key_env &&
            !stored.api_key_file && !endpoint.stored_key;
    }

    function render() {
        var holder = byId("endpoint-list");
        clear(holder);
        var endpoints = state.endpoints;
        if (!endpoints.length || endpoints.every(placeholder)) {
            holder.appendChild(el("div", {className: "empty"}, [
                el("h2", {text: "Set up the AI model that describes your pictures"}),
                el("p", {text: "Choose a model server running on this computer or your " +
                    "network (llama.cpp, LM Studio, Ollama, vLLM), or a cloud service such " +
                    "as OpenAI, Claude or Gemini. Local servers keep your pictures at home; " +
                    "cloud services are easy to start with but charge per picture."}),
                button("Add endpoint", "primary", function () {
                    openDialog("add");
                })
            ]));
            return;
        }
        var table = el("table", {className: "list"});
        table.appendChild(el("thead", null, [el("tr", null, [
            el("th", {text: "Endpoint"}), el("th", {text: "Service"}), el("th", {text: "Model"}),
            el("th", {text: "API key"}), el("th", {text: "Status"}), el("th", {text: "Used by"}),
            el("th", {text: "Actions", className: "cell-actions"})
        ])]));
        var tbody = el("tbody");
        endpoints.forEach(function (endpoint) {
            tbody.appendChild(endpointRow(endpoint));
        });
        table.appendChild(tbody);
        holder.appendChild(el("div", {className: "table-wrap"}, [table]));
        holder.appendChild(el("p", {className: "hint"}, [
            "The status of model servers is checked every " + (STATUS_REFRESH / 1000) +
            " seconds while this page is open. ",
            button("Check again", "subtle small", function () {
                statusRetries = 0;
                reload();
            })
        ]));
    }

    function endpointRow(endpoint) {
        var stored = endpoint.stored || {};
        var nameCell = el("td", null, [
            el("div", {className: "cell-main"}, [
                endpoint.name, " ",
                endpoint.default ? el("span", {className: "pill info plain", text: "Default"}) : null
            ]),
            endpoint.label && endpoint.label !== endpoint.name ?
                el("div", {className: "cell-sub", text: endpoint.label}) : null,
            endpoint.group && endpoint.group !== endpoint.name ?
                el("div", {className: "cell-sub", text: "Group " + endpoint.group}) : null
        ]);
        var serviceCell = el("td", null, [
            el("div", {text: endpoint.preset_label || endpoint.kind}),
            el("div", {className: "cell-sub mono nowrap", text: endpoint.host ||
                                                                endpoint.base_url}),
            endpoint.insecure ? el("div", {className: "cell-sub", text: "Not encrypted (http)"})
                : null
        ]);
        var modelCell = el("td", null, endpoint.model ? [
            el("div", {className: "mono", text: endpoint.model}),
            el("div", {className: "cell-sub", text: fmt.plural(endpoint.concurrency ||
                (endpoint.status && endpoint.status.slots) || 1, "picture") + " at a time" +
                (stored.concurrency ? "" : !endpoint.local ? "" :
                    endpoint.status && endpoint.status.slots ? " (from the server)"
                    : " until the server reports its slots")})
        ] : [el("span", {className: "pill bad plain", text: "Not set"})]);
        var keyCell = el("td", null, [
            el("div", {text: keyText(endpoint), className: keyProblem(endpoint) ? "result-line bad"
                : ""})
        ]);
        var actions = el("td", {className: "cell-actions"}, [
            button("Test", "small", function () {
                testEndpoint(endpoint);
            }, endpoint.local ? "Describe the sample picture to check the settings" :
                "Send the sample picture to " + endpoint.host + " to check the settings"),
            button("Edit", "small", function () {
                openDialog("edit", endpoint.name);
            }),
            !endpoint.default && endpoint.enabled !== false ? button("Make default", "small",
                function () {
                    makeDefault(endpoint);
                }, "Libraries without an endpoint of their own use the default") : null,
            button("Remove", "small subtle danger-text", function () {
                removeEndpoint(endpoint);
            })
        ]);
        var cells = [nameCell, serviceCell, modelCell, keyCell,
                     el("td", null, statusCell(endpoint)), el("td", {text: usedByText(endpoint)})];
        // Narrow screens show each row as a card, labelled from these.
        ["Endpoint", "Service", "Model", "API key", "Status", "Used by"].forEach(function (label, i) {
            cells[i].setAttribute("data-label", label);
        });
        return el("tr", {className: endpoint.enabled === false ? "is-off" : ""},
                  cells.concat([actions]));
    }

    /* ---------- Actions -------------------------------------------------- */

    function makeDefault(endpoint) {
        SHELL.admin("/endpoints/default", {name: endpoint.name}).then(function () {
            SHELL.toast(endpoint.name + " is now the default endpoint.", "ok", 4);
            reload();
        });
    }

    function removeEndpoint(endpoint) {
        var used = endpoint.used_by || [];
        var message = [el("p", {text: "Pictures described by it keep their descriptions."})];
        if (used.length) {
            message.push(el("p", {text: "Used by: " + usedByText(endpoint) + ". Their " +
                "pictures are not described until you choose another endpoint for them on " +
                "the Manage page."}));
        }
        if (endpoint.stored_key) {
            message.push(el("p", {text: "The API key saved with it is deleted from " +
                                        "config.json."}));
        }
        SHELL.confirm({
            title: "Remove the endpoint \u201c" + endpoint.name + "\u201d?",
            message: message,
            confirmText: "Remove endpoint",
            danger: true
        }).then(function (yes) {
            if (!yes) {
                return;
            }
            SHELL.admin("/endpoints/" + encodeURIComponent(endpoint.name) + "/remove", {})
                .then(function (reply) {
                    SHELL.toast("Removed " + endpoint.name + ".", "ok", 4);
                    if (reply.warnings && reply.warnings.length) {
                        showNotice("warn", warningList(reply.warnings));
                    }
                    reload();
                });
        });
    }

    /* ---------- Test ----------------------------------------------------- */

    function money(value) {
        if (value === 0) {
            return "$0";
        }
        return "$" + value.toFixed(value < 0.01 ? 5 : value < 1 ? 3 : 2);
    }

    function parsedValue(value) {
        if (Array.isArray(value)) {
            return value.map(function (item) {
                if (item && typeof item === "object") {
                    return item.name ? item.name + (item.count ? " \u00d7" + item.count : "")
                        : JSON.stringify(item);
                }
                return String(item);
            }).join(", ");
        }
        if (value === true) {
            return "yes";
        }
        if (value === false) {
            return "no";
        }
        if (value && typeof value === "object") {
            return JSON.stringify(value);
        }
        return value === null || value === undefined ? "" : String(value);
    }

    function definitionList(rows) {
        var list = el("dl");
        rows.forEach(function (row) {
            if (row[1] === null || row[1] === undefined || row[1] === "") {
                return;
            }
            list.appendChild(el("dt", {text: row[0]}));
            list.appendChild(el("dd", {text: row[1]}));
        });
        return list;
    }

    function showTestResult(box, endpoint, result) {
        clear(box.body);
        var holder = el("div", {className: "test-result"});
        box.body.appendChild(holder);
        if (result.ok) {
            holder.appendChild(notice("ok", "It works: the model described the sample " +
                                            "picture."));
        } else {
            holder.appendChild(notice("error", [
                el("strong", {text: (ERROR_KINDS[result.error_kind] || "It did not work") + ". "}),
                result.error || ""
            ]));
        }
        var usage = result.usage || {};
        var cost = result.est_cost_per_image;
        var costText = null;
        if (typeof cost === "number") {
            costText = money(cost) + " per picture";
            if (result.pending_images) {
                costText += "; about " + money(result.est_cost_pending) + " for the " +
                    fmt.plural(result.pending_images, "picture") + " still to describe";
            }
            SHELL.storageSet(TEST_COST_KEY + endpoint.name, String(cost));
        } else if (result.local) {
            costText = "Nothing: it runs on your own computer or network";
        } else if (result.ok) {
            costText = "Unknown: add the prices per million tokens in the endpoint's " +
                "settings to see an estimate";
        }
        holder.appendChild(definitionList([
            ["Answer time", typeof result.latency_ms === "number" ?
                (result.latency_ms / 1000).toFixed(1) + " seconds" : null],
            ["Model that answered", result.served_model],
            ["Tokens", usage.input_tokens !== null && usage.input_tokens !== undefined ?
                fmt.number(usage.input_tokens) + " in, " + fmt.number(usage.output_tokens || 0) +
                " out" : null],
            ["Cost", costText],
            ["Picture sent", result.image === "sample picture" ?
                "The sample picture (a chalkboard menu) to " + (result.host || "the server") :
                result.image],
            ["API key", keyText({key_source: result.key_source, key_set: true,
                                 local: result.local})]
        ]));
        if (result.parsed) {
            holder.appendChild(el("h3", {text: "What the model wrote"}));
            var rows = [];
            PARSED_FIELDS.forEach(function (pair) {
                if (pair[0] in result.parsed) {
                    var value = parsedValue(result.parsed[pair[0]]);
                    if (value !== "" && value !== "none" && value !== "unknown") {
                        rows.push([pair[1], value]);
                    }
                }
            });
            holder.appendChild(definitionList(rows));
        }
        if (!result.ok && result.error_kind === "fatal") {
            holder.appendChild(el("p", {className: "muted", text: "Check the API key in the " +
                "endpoint's settings and that the account has credit."}));
        } else if (!result.ok && result.local) {
            holder.appendChild(el("p", {className: "muted", text: "Check that the model server " +
                "is running, that the base URL is right, and that the model can see images " +
                "(for llama.cpp, start it with --mmproj)."}));
        }
    }

    // options.ask: confirm even for a local server (a Test opened from the
    // address must never start by itself).
    function testEndpoint(endpoint, options) {
        options = options || {};
        var ask;
        if (!endpoint.local) {
            ask = SHELL.confirm({
                title: "Send a test picture to " + endpoint.host + "?",
                message: [
                    el("p", {text: "Testing sends one picture, a photo of a chalkboard menu " +
                        "that comes with Image Search, to " + endpoint.preset_label + " (" +
                        endpoint.host + ") and asks the model to describe it. None of your own " +
                        "pictures is sent."}),
                    el("p", {text: "The service may charge for it, usually a fraction of a cent."})
                ],
                confirmText: "Send the test picture"
            });
        } else if (options.ask) {
            ask = SHELL.confirm({
                title: "Test " + endpoint.name + "?",
                message: "The model server at " + (endpoint.host || "this computer") +
                    " describes the sample picture that comes with Image Search (a photo of a " +
                    "chalkboard menu). This can take up to two minutes.",
                confirmText: "Run the test"
            });
        } else {
            ask = Promise.resolve(true);
        }
        ask.then(function (yes) {
            if (!yes) {
                SHELL.setState({dialog: null, name: null});
                return;
            }
            var box = SHELL.modal({
                title: "Test: " + endpoint.name,
                wide: true,
                onClose: function () {
                    SHELL.setState({dialog: null, name: null});
                }
            });
            box.body.appendChild(el("p", null, [
                el("span", {className: "spinner", attrs: {"aria-hidden": "true"}}), " ",
                "Describing the sample picture... This can take up to two minutes, longer " +
                "when the model server is still loading its model."
            ]));
            box.button("Close", "", box.close);
            SHELL.admin("/endpoints/" + encodeURIComponent(endpoint.name) + "/test", {},
                        {quiet: true}).then(function (result) {
                showTestResult(box, endpoint, result);
                reload();
            }, function (error) {
                clear(box.body);
                box.body.appendChild(notice("error", error.message));
            });
        });
    }

    /* ---------- Add / edit dialog ---------------------------------------- */

    function presetOrder() {
        var names = Object.keys(state.presets);
        var local = LOCAL_ORDER.filter(function (name) {
            return names.indexOf(name) >= 0;
        });
        var cloud = CLOUD_ORDER.filter(function (name) {
            return names.indexOf(name) >= 0;
        });
        names.forEach(function (name) {
            if (local.indexOf(name) < 0 && cloud.indexOf(name) < 0 && name !== "custom") {
                (state.presets[name].cloud ? cloud : local).push(name);
            }
        });
        return {local: local, cloud: cloud, other: names.indexOf("custom") >= 0 ? ["custom"] : []};
    }

    function presetSelect(current) {
        var order = presetOrder();
        var select = el("select");
        [["On this computer or your network", order.local], ["Cloud services", order.cloud],
         ["Other", order.other]].forEach(function (group) {
            if (!group[1].length) {
                return;
            }
            var optgroup = el("optgroup", {label: group[0]});
            group[1].forEach(function (name) {
                optgroup.appendChild(el("option", {value: name,
                                                   text: state.presets[name].label}));
            });
            select.appendChild(optgroup);
        });
        select.value = current;
        return select;
    }

    function hostOf(url) {
        var match = /^[a-z][a-z0-9+.-]*:\/\/([^\/?#]+)/i.exec(url || "");
        return match ? match[1].replace(/^[^@]*@/, "") : "";
    }

    function uniqueName(base) {
        var taken = {};
        state.endpoints.forEach(function (endpoint) {
            taken[endpoint.name.toLowerCase()] = true;
        });
        var name = base;
        var number = 2;
        while (taken[name.toLowerCase()]) {
            name = base + "-" + number;
            number += 1;
        }
        return name;
    }

    function textValue(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function endpointDialog(endpoint) {
        var adding = !endpoint;
        var stored = endpoint ? endpoint.stored || {} : {};
        var box = SHELL.modal({
            title: adding ? "Add an endpoint" : "Edit \u201c" + endpoint.name + "\u201d",
            wide: true,
            onClose: function () {
                SHELL.setState({dialog: null, name: null});
            }
        });
        var fields = {};
        var body = box.body;
        var nameTouched = !adding;

        var preset = presetSelect(endpoint ? endpoint.preset : "llamacpp");
        var presetNote = el("p", {className: "preset-note"});
        fields.preset = field("Service", preset, {after: presetNote});
        body.appendChild(fields.preset.node);

        var name = el("input", {type: "text", className: "input", autocomplete: "off",
                                value: endpoint ? endpoint.name : ""});
        fields.name = field("Name", name, {hint: "A short name, used when a library picks " +
                                           "this endpoint."});
        var label = el("input", {type: "text", className: "input", autocomplete: "off",
                                 value: textValue(stored.label)});
        fields.label = field("Label", label, {optional: true, hint: "A longer description, " +
                                              "shown in lists."});
        body.appendChild(el("div", {className: "field-grid"}, [fields.name.node,
                                                                fields.label.node]));

        var model = el("input", {type: "text", className: "input mono", autocomplete: "off",
                                 spellcheck: false, value: textValue(stored.model)});
        var listModels = button("List models", "", null,
                                "Ask the server which models it offers");
        var modelChoices = el("div", {className: "chips", hidden: true});
        fields.model = field("Model", el("div", {className: "field-row"}, [model, listModels]), {
            div: true, control: model, after: modelChoices,
            hint: "The name the service knows the model by. llama.cpp accepts any name (it " +
                "uses the model it has loaded); List models fills it in."
        });
        body.appendChild(fields.model.node);

        var baseUrl = el("input", {type: "text", className: "input mono", autocomplete: "off",
                                   spellcheck: false, value: textValue(stored.base_url)});
        var urlHint = el("span", {className: "field-hint"});
        fields.base_url = field("Base URL", baseUrl, {optional: true, after: urlHint});
        body.appendChild(fields.base_url.node);

        // API key: one of four sources.
        var keyGroup = el("fieldset", {className: "group"}, [el("legend", {text: "API key"})]);
        var modeName = "key-mode-" + Math.random().toString(36).slice(2);
        var modes = {};
        function mode(value, text, extra) {
            var input = el("input", {type: "radio", name: modeName, value: value});
            modes[value] = input;
            var node = el("label", {className: "check"}, [input, el("span", {text: text})]);
            keyGroup.appendChild(node);
            if (extra) {
                keyGroup.appendChild(extra);
            }
            return node;
        }
        var defaultModeText = el("span");
        var envInput = el("input", {type: "text", className: "input mono", autocomplete: "off",
                                    spellcheck: false, value: textValue(stored.api_key_env),
                                    placeholder: "MY_SERVICE_API_KEY"});
        var fileInput = el("input", {type: "text", className: "input mono", autocomplete: "off",
                                     spellcheck: false, value: textValue(stored.api_key_file),
                                     placeholder: "C:\\path\\to\\service.key"});
        var keyInput = el("input", {type: "password", className: "input mono",
                                    autocomplete: "new-password", spellcheck: false,
                                    placeholder: endpoint && endpoint.stored_key ?
                                        "A key is saved; leave empty to keep it" : ""});
        fields.api_key_env = field("Name of the environment variable", envInput);
        fields.api_key_file = field("Key file", fileInput, {hint: "A text file that holds only " +
            "the key. Keep it outside the program folder."});
        fields.api_key = field("API key", keyInput, {hint: "Saved in config.json. It is never " +
            "shown again or sent back to this page."});
        var defaultMode = mode("default", "");
        defaultMode.lastChild.appendChild(defaultModeText);
        mode("env", "From an environment variable (recommended for cloud services)",
             fields.api_key_env.node);
        mode("file", "From a key file", fields.api_key_file.node);
        mode("literal", "Type the key", fields.api_key.node);
        body.appendChild(keyGroup);
        var startMode = stored.api_key_env ? "env" : stored.api_key_file ? "file" :
            endpoint && endpoint.stored_key ? "literal" : "default";
        modes[startMode].checked = true;

        // More settings
        var numbers = {};
        function numberField(key, text, hint) {
            var input = el("input", {type: "text", className: "input", inputMode: "decimal",
                                     autocomplete: "off", value: textValue(stored[key])});
            numbers[key] = input;
            fields[key] = field(text, input, {hint: hint});
            return fields[key].node;
        }
        var extra = el("textarea", {className: "mono", rows: 3, spellcheck: false,
                                    value: stored.extra && Object.keys(stored.extra).length ?
                                        JSON.stringify(stored.extra, null, 2) : "",
                                    placeholder: "{\"temperature\": 0.2}"});
        fields.extra = field("Extra request fields", extra, {optional: true, hint: "A JSON " +
            "object added to every request, for settings this page does not have."});
        var enabled = el("input", {type: "checkbox", checked: stored.enabled !== false});
        fields.enabled = {node: null, error: el("span"), control: enabled};
        var more = el("details", {className: "more"}, [
            el("summary", {text: "More settings"}),
            el("div", {className: "field-grid"}, [
                numberField("concurrency", "Pictures at a time", "How many requests run at " +
                            "once."),
                numberField("max_image_edge", "Picture size sent (pixels)", "The longest side; " +
                            "default 1536."),
                numberField("timeout", "Time limit per picture (seconds)", "Default 180."),
                numberField("max_output_tokens", "Longest answer (tokens)", "Leave empty for " +
                            "the default.")
            ]),
            el("div", {className: "field-grid"}, [
                numberField("price_in", "Price per million input tokens ($)", "Only used to " +
                            "estimate costs."),
                numberField("price_out", "Price per million output tokens ($)", "Only used " +
                            "to estimate costs.")
            ]),
            fields.extra.node,
            el("label", {className: "check"}, [enabled, el("span", null, [
                "Turned on", el("span", {className: "field-hint", text: "A turned-off " +
                    "endpoint is not used; libraries that name it are not described."})
            ])])
        ]);
        body.appendChild(more);

        var general = el("span", {className: "foot-note field-error", hidden: true});
        box.footer.appendChild(general);
        box.button("Cancel", "", box.close);
        var save = box.button(adding ? "Add endpoint" : "Save changes", "primary", submit);

        var initial = null;

        function info() {
            return state.presets[preset.value] || {};
        }

        function presetChanged() {
            var chosen = info();
            var host = hostOf(baseUrl.value.trim() || chosen.base_url);
            clear(presetNote);
            presetNote.appendChild(document.createTextNode(chosen.notes || ""));
            if (chosen.cloud) {
                presetNote.appendChild(el("strong", {text: " Pictures are sent to " +
                    chosen.label + (host ? " (" + host + ")" : "") + "; " + chosen.label +
                    "'s API terms apply."}));
            }
            baseUrl.placeholder = chosen.base_url || "https://...";
            urlHint.textContent = chosen.base_url ? "Leave empty to use " + chosen.base_url + "." :
                "Required for this service: the address of its API.";
            defaultModeText.textContent = chosen.key_env ? "The usual environment variable, " +
                chosen.key_env + (chosen.cloud ? "" : " (or none)") : "No key (local servers " +
                "usually need none)";
            numbers.concurrency.placeholder = chosen.concurrency ? String(chosen.concurrency) :
                "From the server";
            numbers.max_image_edge.placeholder = "1536";
            numbers.timeout.placeholder = "180";
            if (adding && !nameTouched) {
                name.value = uniqueName(preset.value === "anthropic" ? "claude" : preset.value);
            }
            keyChanged();
        }

        function keyChanged() {
            fields.api_key_env.node.hidden = !modes.env.checked;
            fields.api_key_file.node.hidden = !modes.file.checked;
            fields.api_key.node.hidden = !modes.literal.checked;
        }

        function current() {
            var data = {
                preset: preset.value,
                name: name.value.trim(),
                label: label.value.trim(),
                model: model.value.trim(),
                base_url: baseUrl.value.trim(),
                extra: extra.value.trim(),
                enabled: enabled.checked
            };
            Object.keys(numbers).forEach(function (key) {
                data[key] = numbers[key].value.trim();
            });
            return data;
        }

        function keyFields() {
            var data = {};
            var hadKey = Boolean(endpoint && endpoint.stored_key);
            if (modes.literal.checked) {
                data.api_key_env = "";
                data.api_key_file = "";
                if (keyInput.value.trim()) {
                    data.api_key = keyInput.value.trim();
                }
                return data;
            }
            if (hadKey && !modes[startMode].checked) {
                // Switched away from a typed key: it is removed from config.json.
                data.clear_api_key = true;
            }
            data.api_key_env = modes.env.checked ? envInput.value.trim() : "";
            data.api_key_file = modes.file.checked ? fileInput.value.trim() : "";
            return data;
        }

        function collect() {
            var data = current();
            if (!adding) {
                Object.keys(initial).forEach(function (key) {
                    if (data[key] === initial[key]) {
                        delete data[key];
                    }
                });
            }
            var keys = keyFields();
            Object.keys(keys).forEach(function (key) {
                if (adding || key === "api_key" || key === "clear_api_key" ||
                        keys[key] !== textValue(stored[key])) {
                    data[key] = keys[key];
                }
            });
            if (!adding && !keys.clear_api_key && !keys.api_key &&
                    keys.api_key_env === textValue(stored.api_key_env) &&
                    keys.api_key_file === textValue(stored.api_key_file)) {
                delete data.api_key_env;
                delete data.api_key_file;
            }
            return data;
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
                if (!target) {
                    return;
                }
                target.error.textContent = error.fields[key];
                target.error.hidden = false;
                target.control.setAttribute("aria-invalid", "true");
                if (target.node && target.node.closest("details")) {
                    more.open = true;
                }
                if (key === "api_key_env" || key === "api_key_file" || key === "api_key") {
                    modes[key === "api_key_env" ? "env" : key === "api_key_file" ? "file"
                        : "literal"].checked = true;
                    keyChanged();
                }
                if (!shown) {
                    target.control.focus();
                }
                shown = true;
            });
            if (!shown) {
                general.textContent = error.message;
                general.hidden = false;
            }
        }

        function checkExtra() {
            var text = extra.value.trim();
            if (!text) {
                return true;
            }
            try {
                var value = JSON.parse(text);
                if (value && typeof value === "object" && !Array.isArray(value)) {
                    return true;
                }
            } catch (error) {
                // Reported below.
            }
            showErrors({fields: {extra: "Not a JSON object, such as {\"temperature\": 0.2}."}});
            return false;
        }

        function submit() {
            clearErrors();
            if (!checkExtra()) {
                return;
            }
            if (!model.value.trim()) {
                showErrors({fields: {model: "Enter the model's name. For llama.cpp any name " +
                    "works, such as the model file's name; List models fills it in."}});
                return;
            }
            var data = collect();
            if (!adding && !Object.keys(data).length) {
                box.close();
                return;
            }
            save.disabled = true;
            SHELL.admin("/endpoints", {endpoint: data, original_name: adding ? null :
                                       endpoint.name}, {quiet: true}).then(function (reply) {
                box.close();
                SHELL.toast((adding ? "Added " : "Saved ") + reply.endpoint.name + "." +
                            (reply.endpoint.default ? " It is the default endpoint." : ""),
                            "ok", 5);
                if (reply.warnings && reply.warnings.length) {
                    showNotice("warn", [el("strong", {text: reply.endpoint.name + ": "}),
                                        warningList(reply.warnings)]);
                }
                reload();
            }, function (error) {
                save.disabled = false;
                if (error.status !== 409) {
                    showErrors(error);
                }
            });
        }

        listModels.addEventListener("click", function () {
            var data = current();
            Object.keys(data).forEach(function (key) {
                if (data[key] === "" && key !== "name") {
                    delete data[key];
                }
            });
            delete data.extra;
            delete data.enabled;
            var keys = keyFields();
            Object.keys(keys).forEach(function (key) {
                if (keys[key] !== "" || key === "clear_api_key") {
                    data[key] = keys[key];
                }
            });
            listModels.disabled = true;
            listModels.textContent = "Asking...";
            clear(modelChoices);
            SHELL.admin("/endpoints/models", {endpoint: data, original_name: adding ? null :
                                              endpoint.name}, {quiet: true}).then(function (reply) {
                var models = reply.models || [];
                if (!models.length) {
                    modelChoices.appendChild(el("span", {className: "field-hint",
                        text: "The server did not list any models; type the name instead."}));
                }
                models.slice(0, 60).forEach(function (item) {
                    var text = typeof item === "string" ? item : item.id || item.name || "";
                    if (!text) {
                        return;
                    }
                    modelChoices.appendChild(button(text, "small", function () {
                        model.value = text;
                    }, "Use this model"));
                });
                modelChoices.hidden = false;
            }, function (error) {
                modelChoices.appendChild(el("span", {className: "field-error", text: error.message}));
                modelChoices.hidden = false;
            }).then(function () {
                listModels.disabled = false;
                listModels.textContent = "List models";
            });
        });

        preset.addEventListener("change", presetChanged);
        baseUrl.addEventListener("change", presetChanged);
        name.addEventListener("input", function () {
            nameTouched = name.value.trim() !== "";
        });
        Object.keys(modes).forEach(function (key) {
            modes[key].addEventListener("change", keyChanged);
        });
        presetChanged();
        initial = current();
        box.submitOnEnter(function () {
            if (!save.disabled) {
                submit();
            }
        });
        if (adding) {
            preset.focus();
        }
        return box;
    }

    /* ---------- Dialogs from the URL ------------------------------------- */

    // fromAddress: opened from ?dialog=... rather than by a click.
    function openDialog(kind, name, fromAddress) {
        var endpoint = name ? findEndpoint(name) : null;
        if (kind !== "add" && !endpoint) {
            return;
        }
        SHELL.setState({dialog: kind, name: endpoint ? endpoint.name : null});
        if (kind === "add") {
            endpointDialog(null);
        } else if (kind === "edit") {
            endpointDialog(endpoint);
        } else if (kind === "test") {
            testEndpoint(endpoint, {ask: fromAddress});
        }
    }

    function init() {
        byId("add-endpoint").addEventListener("click", function () {
            openDialog("add");
        });
        load().then(function () {
            var wanted = SHELL.getState();
            if (wanted.dialog) {
                openDialog(wanted.dialog, wanted.name, true);
            }
        }, function () {
            // Shown already.
        });
        // Model servers are started and stopped outside this page: look again
        // now and then while the page is visible.
        window.setInterval(function () {
            if (!document.hidden && state) {
                reload();
            }
        }, STATUS_REFRESH);
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden && state) {
                reload();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
