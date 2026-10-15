/* Tests for the Export dialog of the Manage page, under Node (no packages needed): the
   real page scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake
   fetch() that gives the answers of the server tests/test_admin_export.py set up (a
   library whose saved export options hold a footer line that is too long and an option
   that does not exist). The saved options an export cannot use are named once, in a
   notice at the top of the options; the footer line shows in its field as saved, and
   the review is refused until it is shorter; the review names what its preview leaves
   out, each line once.

   Run by tests/test_admin_export.py (which writes ANSWERS.json), or directly:
       node tests/js/manage_export_test.js ANSWERS.json
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;

var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");
var ANSWERS = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
var SCRIPTS = ["site.js", "shell.js", "export-review.js", "place-names.js", "street-maps.js",
               "manage.js"];

function response(status, body) {
    var text = JSON.stringify(body);
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status: status,
        headers: {get: function () {
            return null;
        }},
        body: null,
        json: function () {
            return Promise.resolve().then(function () {
                return JSON.parse(text);
            });
        }
    });
}

/* The Manage page at ?searchText, answer(path, body) for each request (a promise of a
   response, or null: 404). page.log.sent: [[path, body]] of the requests with a body. */
function managePage(searchText, answer) {
    var log = {requests: [], sent: []};
    var document = FAKE.makeDocument();
    var create = document.createElement;
    document.createElement = function (tag) {
        var node = create.apply(document, arguments);
        // (What the made-up DOM leaves out: an input's empty value, select().)
        if (!node.select) {
            node.select = function () {};
        }
        if (String(tag).toLowerCase() === "input" && node.value === undefined) {
            node.value = "";
        }
        return node;
    };
    var config = {mode: "server", page: "manage", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Family"],
                  libraryList: [{id: "lib_family1", name: "Family"}],
                  capabilities: {admin: true, static: false, people: false}};
    FAKE.parseBody(document, fs.readFileSync(path.join(ROOT, "templates", "manage.html"),
                                             "utf8"), config);
    function storage() {
        var items = {};
        return {
            getItem: function (key) {
                return Object.prototype.hasOwnProperty.call(items, key) ? items[key] : null;
            },
            setItem: function (key, value) {
                items[key] = String(value);
            },
            removeItem: function (key) {
                delete items[key];
            }
        };
    }
    var location = {search: searchText || "", hash: "", pathname: "/manage", protocol: "http:",
                    href: "http://127.0.0.1:5000/manage" + (searchText || ""),
                    origin: "http://127.0.0.1:5000"};
    function moved(url) {
        var parts = String(url).split("?");
        location.pathname = parts[0];
        location.search = parts.length > 1 ? "?" + parts[1] : "";
    }
    var history = {state: null, scrollRestoration: "auto",
                   pushState: function (state, title, url) {
                       moved(url);
                   },
                   replaceState: function (state, title, url) {
                       moved(url);
                   },
                   back: function () {}};
    function fetch(url, init) {
        log.requests.push(url);
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (body) {
            log.sent.push([String(url), body]);
        }
        return answer(String(url), body) || response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: location,
        history: history,
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1400,
        innerHeight: 900,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        scrollBy: function () {},
        matchMedia: function () {
            return {matches: false, addEventListener: function () {}, addListener: function () {}};
        },
        requestAnimationFrame: function (callback) {
            return setTimeout(callback, 0);
        },
        cancelAnimationFrame: clearTimeout,
        getComputedStyle: function () {
            return {getPropertyValue: function () {
                return "";
            }};
        },
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
        // (Nothing repeats in a test: the page's own clocks are left out.)
        setInterval: function () {
            return 0;
        },
        clearInterval: function () {},
        console: {log: function () {}, warn: function () {}, error: function (text) {
            log.error = String(text);
        }},
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    SCRIPTS.forEach(function (name) {
        vm.runInContext(fs.readFileSync(path.join(STATIC, name), "utf8"), context,
                        {filename: name});
    });
    return {context: context, document: document, log: log, location: location};
}

/* The server of tests/test_admin_export.py: GET /state, and the preview: refused while
   the footer line is too long, else its answer (extra: more option_warnings lines). */
function answers(extra) {
    return function (url, body) {
        if (url.indexOf("/api/admin/state") === 0) {
            return response(200, ANSWERS.state);
        }
        if (url.indexOf("/api/admin/jobs") === 0 && !body) {
            return response(200, ANSWERS.state.jobs);
        }
        if (/\/export\/preview$/.test(url)) {
            if (((body.options || {}).credits || "").length > 4000) {
                return response(400, ANSWERS.refused);
            }
            var reply = JSON.parse(JSON.stringify(ANSWERS.preview));
            reply.option_warnings = reply.option_warnings.concat(extra || []);
            return response(200, reply);
        }
        return null;
    };
}

function dialog(page) {
    var open = page.document.querySelectorAll("dialog");
    return open[open.length - 1] || null;
}

function warnings(node) {
    return node.querySelectorAll(".export-option-warnings");
}

function footerButton(page, text) {
    return dialog(page).querySelectorAll(".modal-foot button").filter(function (button) {
        return button.textContent.indexOf(text) === 0;
    })[0];
}

function creditsInput(page) {
    // (The second text field of "The site": the title, then the footer line; null
    // while the review is shown.)
    var site = dialog(page).querySelectorAll("fieldset").filter(function (set) {
        var legend = set.querySelector("legend");
        return legend && legend.textContent === "The site";
    })[0];
    return site ? site.querySelectorAll(".field-grid input")[1] : null;
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

var SAVED = ANSWERS.state.libraries[0].export_info.option_warnings;

test("the saved options an export cannot use: one notice at the top of the options",
     function () {
         assert.strictEqual(SAVED.length, 2, JSON.stringify(SAVED));
         var page = managePage("?dialog=export&id=lib_family1", answers());
         return waitFor(function () {
             return dialog(page) && warnings(dialog(page)).length;
         }).then(function () {
             var found = warnings(dialog(page));
             assert.strictEqual(found.length, 1);
             var box = found[0];
             assert.strictEqual(box.getAttribute("role"), "status");
             assert.ok(box.classList.contains("warn"));
             // First in the dialog's body, after its words of introduction.
             var body = dialog(page).querySelector(".modal-body");
             var first = body.children.filter(function (node) {
                 return node.tagName !== "P";
             })[0];
             assert.strictEqual(first, box);
             assert.deepStrictEqual(box.querySelectorAll("li").map(function (item) {
                 return item.textContent;
             }), SAVED);
             assert.ok(/too long is shown below as saved/.test(box.textContent),
                       box.textContent);
             // The footer line is in its field as saved, to be shortened.
             assert.strictEqual(creditsInput(page).value, ANSWERS.long);
         });
     });

test("a footer line too long: the review is refused until it is shorter, then names "
     + "what its preview leaves out, once each", function () {
    var page = managePage("?dialog=export&id=lib_family1",
                          answers([ANSWERS.preview.option_warnings[0]]));
    var credits;
    return waitFor(function () {
        return dialog(page) && warnings(dialog(page)).length;
    }).then(function () {
        footerButton(page, "Review what will be published").click();
        return waitFor(function () {
            credits = creditsInput(page);
            return credits && credits.getAttribute("aria-invalid") === "true";
        });
    }).then(function () {
        var shown = dialog(page).querySelectorAll(".field-error").filter(function (node) {
            return !node.hidden && /4000 characters/.test(node.textContent);
        });
        assert.strictEqual(shown.length >= 1, true);
        var previews = page.log.sent.filter(function (entry) {
            return /\/export\/preview$/.test(entry[0]) && entry[1].out_dir !== undefined;
        });
        assert.strictEqual(previews[0][1].options.credits, ANSWERS.long);
        credits.value = "Me";
        credits.dispatchEvent({type: "input"});
        footerButton(page, "Review what will be published").click();
        return waitFor(function () {
            return dialog(page).querySelector(".export-headline") &&
                warnings(dialog(page)).length;
        });
    }).then(function () {
        var found = warnings(dialog(page));
        assert.strictEqual(found.length, 1);
        assert.strictEqual(found[0].getAttribute("role"), "status");
        var summary = dialog(page).querySelector(".export-summary");
        assert.strictEqual(summary.firstChild, found[0]);
        // The preview's lines (the option that does not exist), once each; the
        // footer line the dialog replaces is not one of them.
        assert.deepStrictEqual(ANSWERS.preview.option_warnings.length, 1);
        assert.ok(/"colour"/.test(found[0].textContent), found[0].textContent);
        assert.ok(!/"credits"/.test(found[0].textContent), found[0].textContent);
        assert.strictEqual(found[0].textContent.split("\"colour\"").length - 1, 1);
        var last = page.log.sent.filter(function (entry) {
            return /\/export\/preview$/.test(entry[0]);
        }).pop();
        assert.strictEqual(last[1].options.credits, "Me");
    });
});

var failed = 0;
tests.reduce(function (chain, entry) {
    return chain.then(function () {
        return Promise.resolve().then(entry.body).then(function () {
            console.log("ok   " + entry.name);
        }, function (error) {
            failed += 1;
            console.log("FAIL " + entry.name + "\n     " + (error && error.stack || error));
        });
    });
}, Promise.resolve()).then(function () {
    console.log((tests.length - failed) + " of " + tests.length + " passed");
    process.exit(failed ? 1 : 0);
});
