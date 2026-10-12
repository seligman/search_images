/* Tests for search by an example picture on the server's search page
   (static/app.js with static/site.js), as the admin sees it: the real
   scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake
   fetch() for the server's answers (/api/index, /api/looks, /api/looks/query,
   ...). The pictures, vectors and answers are all made up here.

   Run by tests/test_looks.py, or directly:
       node tests/js/app_looks_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var settle = FAKE.settle;
var byId = FAKE.byId;
var search = FAKE.search;
var notes = FAKE.notes;

var DIMS = 8;
var LOOK_MODEL = "l-0123456789ab";
var THUMB = "data:image/jpeg;base64,AAAA";

function unit(values) {
    var norm = Math.sqrt(values.reduce(function (sum, value) {
        return sum + value * value;
    }, 0));
    return values.map(function (value) {
        return Math.round(value / norm * 127);
    });
}

/* A look vector whose cosine with axis 0 is cos (the rest on another axis). */
function look(cos, axis) {
    var values = [0, 0, 0, 0, 0, 0, 0, 0];
    values[0] = cos;
    values[axis] = Math.sqrt(Math.max(0, 1 - cos * cos));
    return unit(values);
}

function base64(numbers) {
    return Buffer.from(Int8Array.from(numbers).buffer).toString("base64");
}

/* 40 pictures: "Lighthouse" (pic1) and "Lighthouse at dusk" (pic2) look like
   the example picture (axis 0), the others do not (their looks spread over
   the other axes). */
function pictures() {
    var records = [];
    var looks = [];
    for (var k = 0; k < 40; k++) {
        var title = k === 1 ? "Lighthouse" : k === 2 ? "Lighthouse at dusk" : "Beach " + k;
        records.push({i: "pic" + k, l: "lib", t: title, d: "A picture of " + title + ".",
                      g: [k < 3 ? "lighthouse" : "beach"], ds: 1, dk: "2020-01-" + (10 + k % 20)});
        looks.push(k === 1 ? look(0.98, 1) : k === 2 ? look(0.95, 2)
            : look(0.45 + 0.3 * ((k * 7) % 40) / 40, 3 + k % 5));
    }
    return {records: records, looks: looks};
}

function response(status, body, headers) {
    var text = typeof body === "string" ? body : JSON.stringify(body);
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status: status,
        headers: {get: function (name) {
            return (headers || {})[name] || null;
        }},
        body: null,
        json: function () {
            return Promise.resolve().then(function () {
                return JSON.parse(text);
            });
        }
    });
}

/* The admin's search page on the server. options: {looks: false (the server
   has none)}. page.answers: what /api/looks/query answers next ([status,
   body], in order); page.log.requests: the URLs asked for. */
function serverPage(options) {
    options = options || {};
    var data = pictures();
    var log = {requests: [], blobs: 0, revoked: []};
    var answers = [];
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        var node = document.createElement(tag);
        node.namespaceURI = ns;
        return node;
    };
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false}};
    FAKE.parseBody(document, FAKE.PAGE, config);
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
    function fetch(url, init) {
        log.requests.push(url);
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  records: data.records}, {ETag: "\"index-1\""});
        }
        if (plain === "/api/vectors") {
            // (Meaning vectors too, so a typed query could ask /api/embed.)
            return response(200, {model: "m-1", dims: DIMS, query: true,
                                  ids: data.records.map(function (rec) {
                                      return rec.i;
                                  }),
                                  data: base64([].concat.apply([], data.looks))});
        }
        if (plain === "/api/embed") {
            return response(200, {model: "m-1", dims: DIMS, vec: base64(look(0.2, 4))});
        }
        if (plain === "/api/looks") {
            if (options.looks === false) {
                return response(200, {version: 1, model: "", dims: 0, query: false, count: 0,
                                      ids: [], data: ""});
            }
            return response(200, {version: 1, model: LOOK_MODEL, dims: DIMS, query: true,
                                  count: data.records.length,
                                  ids: data.records.map(function (rec) {
                                      return rec.i;
                                  }),
                                  data: base64([].concat.apply([], data.looks))},
                            {ETag: "\"looks-1\""});
        }
        if (plain === "/api/looks/query") {
            log.sent = init && init.body;
            var next = answers.shift() || [503, {state: "unavailable", error: "No answer."}];
            return response(next[0], next[1]);
        }
        return response(404, {error: "not here"});
    }
    var PageURL = class extends URL {};
    PageURL.createObjectURL = function () {
        log.blobs += 1;
        return "blob:page/" + log.blobs;
    };
    PageURL.revokeObjectURL = function (url) {
        log.revoked.push(url);
    };
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/", origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state) {
            this.state = state;
        }, replaceState: function (state) {
            this.state = state;
        }, back: function () {}},
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
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function (text) {
            log.error = String(text);
        }},
        atob: function (text) {
            return Buffer.from(text, "base64").toString("binary");
        },
        btoa: function (text) {
            return Buffer.from(text, "binary").toString("base64");
        },
        URLSearchParams: URLSearchParams,
        URL: PageURL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log, answers: answers, data: data};
}

function count(page) {
    return byId(page, "result-count").textContent;
}

function chips(page) {
    return byId(page, "active-filters").querySelectorAll(".example-chip");
}

function toasts(page) {
    var region = byId(page, "shell-toasts");
    return region ? region.textContent : "";
}

function picture(name, type) {
    return {name: name, type: type || "image/jpeg", size: 2000};
}

function drop(page, file) {
    var event = {type: "drop", dataTransfer: {types: ["Files"], files: [file]}};
    page.document.dispatchEvent(event);
    return event;
}

function paste(page, target, file, types) {
    var event = {type: "paste", target: target,
                 clipboardData: {types: types || ["Files"], files: [file]}};
    page.document.dispatchEvent(event);
    return event;
}

function ready() {
    return [200, {model: LOOK_MODEL, dims: DIMS, vec: base64(look(0.99, 7)), matches: true,
                  thumb: THUMB}];
}

// The page has loaded its pictures and the look vectors (the button shows).
function loaded(page) {
    return waitFor(function () {
        var button = byId(page, "search-box").querySelector(".example-button");
        return /\d+ pictures?/.test(count(page)) && button && !button.hidden;
    });
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Tests ------------------------------------------------------------------- */

test("a dropped picture: the search stays until it is compared, then goes by its looks",
     function () {
         var page = serverPage();
         page.answers.push(ready());
         return loaded(page).then(function () {
             search(page, "beach");
             return waitFor(function () {
                 return count(page) === "38 pictures";
             });
         }).then(function () {
             drop(page, picture("lighthouse.jpg"));
             // Until the server has answered, the search is the one before.
             assert.strictEqual(count(page), "38 pictures");
             assert.strictEqual(byId(page, "query").value, "beach");
             assert.ok(/Comparing/.test(chips(page)[0].textContent));
             return waitFor(function () {
                 return /Looks like/.test(byId(page, "active-filters").textContent);
             });
         }).then(function () {
             // The words typed before went; the pictures that look like it came.
             assert.strictEqual(byId(page, "query").value, "");
             assert.strictEqual(count(page), "2 pictures");
             assert.ok(/looking like your picture/.test(notes(page)), notes(page));
             var chip = chips(page);
             assert.strictEqual(chip.length, 1);
             assert.strictEqual(chip[0].querySelector("img").src, THUMB);
             // (The server's small picture is shown: the file's own blob: URL goes.)
             assert.deepStrictEqual(page.log.revoked, ["blob:page/1"]);
             // Words typed now choose among them, and are not sent to be embedded.
             var asked = page.log.requests.filter(function (url) {
                 return /\/api\/embed/.test(url);
             }).length;
             search(page, "dusk");
             return waitFor(function () {
                 return count(page) === "1 picture";
             }).then(function () {
                 assert.ok(/among those matching/.test(notes(page)), notes(page));
                 return settle(50);
             }).then(function () {
                 assert.strictEqual(page.log.requests.filter(function (url) {
                     return /\/api\/embed/.test(url);
                 }).length, asked);
             });
         });
     });

test("a picture that cannot be compared leaves the search as it was, with a message",
     function () {
         var page = serverPage();
         page.answers.push(ready());
         return loaded(page).then(function () {
             drop(page, picture("lighthouse.jpg"));
             return waitFor(function () {
                 return count(page) === "2 pictures";
             });
         }).then(function () {
             // The next one is still being compared: the search is the first one's.
             page.answers.push([503, {state: "starting", error: "Loading."}]);
             page.answers.push([400, {error: "This picture cannot be read: cannot decode " +
                                             "image."}]);
             drop(page, picture("broken.jpg"));
             var list = chips(page);
             assert.strictEqual(list.length, 2);
             assert.ok(/Looks like/.test(list[0].textContent));
             assert.ok(/Comparing/.test(list[1].textContent));
             assert.ok(/comparing your new picture/.test(notes(page)), notes(page));
             assert.strictEqual(count(page), "2 pictures");
             return waitFor(function () {
                 return /could not be compared/.test(toasts(page));
             }, 8000);
         }).then(function () {
             assert.ok(/cannot decode image/.test(toasts(page)), toasts(page));
             var list = chips(page);
             assert.strictEqual(list.length, 1);
             assert.strictEqual(list[0].querySelector("img").src, THUMB);
             assert.strictEqual(count(page), "2 pictures");
             // Taking the picture away searches without it.
             list[0].click();
             return waitFor(function () {
                 return count(page) === "40 pictures";
             });
         }).then(function () {
             assert.strictEqual(chips(page).length, 0);
         });
     });

test("a picture the browser cannot show gets a symbol in its chip", function () {
    var page = serverPage();
    return loaded(page).then(function () {
        page.answers.push([503, {state: "starting", error: "Loading."}]);
        drop(page, picture("photo.heic", "image/heic"));
        var image = chips(page)[0].querySelector("img");
        assert.ok(image);
        image.dispatchEvent({type: "error"});
        assert.strictEqual(chips(page)[0].querySelector("img"), null);
        assert.ok(chips(page)[0].querySelector(".example-icon"));
        // The pending chip stops the comparing.
        chips(page)[0].click();
        assert.strictEqual(chips(page).length, 0);
        assert.deepStrictEqual(page.log.revoked, ["blob:page/1"]);
    });
});

test("a paste is an example picture only outside text fields", function () {
    var page = serverPage();
    var sent = function () {
        return page.log.requests.filter(function (url) {
            return /\/api\/looks\/query/.test(url);
        }).length;
    };
    return loaded(page).then(function () {
        var file = picture("pasted.png", "image/png");
        // Into a text field (the viewer's Edit form, say): the text is pasted.
        var area = page.document.createElement("textarea");
        assert.strictEqual(paste(page, area, file, ["text/plain", "Files"]).defaultPrevented,
                           undefined);
        var editable = page.document.createElement("div");
        editable.isContentEditable = true;
        assert.strictEqual(paste(page, editable, file).defaultPrevented, undefined);
        // Into the search box with text on the clipboard too: the text.
        var box = byId(page, "query");
        assert.strictEqual(paste(page, box, file, ["text/plain", "Files"]).defaultPrevented,
                           undefined);
        // While the viewer is open: nothing.
        var isOpen = page.context.VIEWER.isOpen;
        page.context.VIEWER.isOpen = function () {
            return true;
        };
        assert.strictEqual(paste(page, page.document.body, file).defaultPrevented, undefined);
        page.context.VIEWER.isOpen = isOpen;
        assert.strictEqual(sent(), 0);
        assert.strictEqual(chips(page).length, 0);
        // A picture alone, into the search box or the page: searched by.
        page.answers.push([503, {state: "starting", error: "Loading."}]);
        assert.strictEqual(paste(page, box, file).defaultPrevented, true);
        return waitFor(function () {
            return sent() === 1;
        });
    }).then(function () {
        assert.strictEqual(chips(page).length, 1);
    });
});

test("nothing close enough: the empty results say so, and offer to drop the words",
     function () {
         var page = serverPage();
         page.answers.push(ready());
         return loaded(page).then(function () {
             drop(page, picture("lighthouse.jpg"));
             return waitFor(function () {
                 return count(page) === "2 pictures";
             });
         }).then(function () {
             search(page, "birthday");
             return waitFor(function () {
                 return count(page) === "0 pictures";
             });
         }).then(function () {
             var empty = byId(page, "empty");
             assert.ok(/No picture looks close enough to yours/.test(empty.textContent),
                       empty.textContent);
             assert.ok(/choose among the pictures that look like it/.test(empty.textContent));
             assert.ok(!/spelling|meaning/i.test(empty.textContent), empty.textContent);
             var remove = empty.querySelectorAll("button").filter(function (button) {
                 return button.textContent === "Remove the words";
             })[0];
             assert.ok(remove, empty.textContent);
             remove.click();
             return waitFor(function () {
                 return count(page) === "2 pictures";
             });
         });
     });

test("look: where there are no look vectors says why", function () {
    var page = serverPage({looks: false});
    return waitFor(function () {
        return /\d+ pictures?/.test(count(page));
    }).then(function () {
        // (No look vectors: no example picture button either.)
        assert.ok(byId(page, "search-box").querySelector(".example-button").hidden);
        search(page, "look:pic1");
        return waitFor(function () {
            return /needs the pictures' look vectors/.test(byId(page, "query-notes").textContent);
        });
    }).then(function () {
        assert.strictEqual(count(page), "1 picture");
        assert.ok(/Update looks/.test(byId(page, "query-notes").textContent));
    });
});

/* ---------- Run ---------------------------------------------------------------------- */

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
