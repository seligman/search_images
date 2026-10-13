/* Tests for favorites, ratings and albums on the server's search page as the
   admin sees it (static/app.js, viewer.js, site.js, shell.js): the real
   scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake
   fetch() for the server's answers (/api/index and the admin routes of
   albums.py). The album picker (Enter adds and never takes out; a new
   album only when no album matches; the focus stays where it was), the
   keys of the marks (by the record, so they work while the pane shows the
   edit form), the focus after a star is pressed, and Save as album (not
   for a visibility switch alone; Update for the album whose search the
   Albums page opened). The pictures and albums are all made up here.

   Run by tests/test_admin_albums.py, or directly:
       node tests/js/app_albums_test.js
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

function records() {
    return [
        {i: 1, l: "lib", t: "Lighthouse", g: ["lighthouse"], ds: 1, dk: "2020-01-10", al: [1]},
        {i: 2, l: "lib", t: "Beach", g: ["beach"], ds: 1, dk: "2020-01-11"},
        {i: 3, l: "lib", t: "Sunset", g: ["sunset", "beach"], ds: 1, dk: "2020-01-12", rt: 3}
    ];
}

function albums() {
    return [
        {id: 2, name: "Pets", kind: "manual", cover: null, items: []},
        {id: 1, name: "Summer trip", kind: "manual", cover: null, items: [1]},
        {id: 3, name: "Sunsets", kind: "manual", cover: null, items: []},
        {id: 12, name: "Water", kind: "smart", cover: null, query: "beach", options: {}}
    ];
}

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

/* The admin's search page on the server; search: its address's ?... part.
   page.log.sent: [[path, body]] of the admin requests made. */
function serverPage(searchText) {
    var log = {requests: [], sent: []};
    var data = {records: records(), albums: albums()};
    var marked = {3: {favorite: false, rating: 3}};
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
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: data.albums, records: data.records});
        }
        var admin = /^\/api\/admin(\/.*)$/.exec(plain);
        if (!admin) {
            return response(404, {error: "not here"});
        }
        var route = admin[1];
        log.sent.push([route, body]);
        var items = /^\/albums\/(\d+)\/items$/.exec(route);
        if (items) {
            return response(200, {added: (body.add || []).length,
                                  removed: (body.remove || []).length});
        }
        if (route === "/albums" && body) {
            return response(201, {album: {id: 50, name: body.name, kind: body.kind || "manual",
                                          cover: null, query: body.query || "",
                                          options: body.options || {}}});
        }
        var one = /^\/albums\/(\d+)$/.exec(route);
        if (one && body) {
            var album = data.albums.filter(function (item) {
                return String(item.id) === one[1];
            })[0];
            return response(200, {album: {id: album.id, name: body.name || album.name,
                                          kind: album.kind, cover: null,
                                          query: body.query, options: body.options}});
        }
        if (route === "/marks") {
            var marks = {};
            body.ids.forEach(function (id) {
                var mark = marked[id] || {favorite: false, rating: 0};
                if (body.favorite !== undefined) {
                    mark.favorite = body.favorite;
                }
                if (body.rating !== undefined) {
                    mark.rating = body.rating;
                }
                marked[id] = marks[id] = mark;
            });
            return response(200, {changed: body.ids.length, found: body.ids.length, unknown: 0,
                                  marks: marks});
        }
        return response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: searchText || "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
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
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log, data: data};
}

function loaded(page) {
    return waitFor(function () {
        return /\d+ pictures?/.test(byId(page, "result-count").textContent);
    });
}

function dialog(page) {
    var open = page.document.querySelectorAll("dialog");
    return open[open.length - 1] || null;
}

// Types into the album picker's box (and presses Enter when enter is true).
function type(page, text, enter) {
    var input = dialog(page).querySelector(".album-picker-find");
    input.value = text;
    input.dispatchEvent({type: "input"});
    if (enter) {
        input.dispatchEvent({type: "keydown", key: "Enter"});
    }
    return input;
}

function note(page) {
    return dialog(page).querySelector(".album-picker-note").textContent;
}

function sent(page) {
    return page.log.sent.map(function (entry) {
        return entry[0] + " " + JSON.stringify(entry[1]);
    });
}

function picker(page, ids) {
    var recs = {};
    page.data.records.forEach(function (rec) {
        recs[rec.i] = rec;
    });
    page.context.VIEWER.marks.picker({ids: ids, albums: page.data.albums, mode: "add",
                                      get: function (id) {
                                          return recs[id] || null;
                                      }});
    return settle(5);
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Tests ------------------------------------------------------------------- */

test("Enter in the album picker adds, and never takes a picture out", function () {
    var page = serverPage();
    return loaded(page).then(function () {
        return picker(page, [1]);
    }).then(function () {
        // Picture 1 is in Summer trip already: Enter says so and changes nothing.
        type(page, "Summer trip", true);
        return settle(20);
    }).then(function () {
        assert.deepStrictEqual(sent(page), []);
        assert.ok(/in Summer trip already/.test(note(page)), note(page));
        // "Su" matches two albums: Enter makes no new album "Su".
        type(page, "Su", true);
        return settle(20);
    }).then(function () {
        assert.deepStrictEqual(sent(page), []);
        assert.ok(/2 albums match/.test(note(page)), note(page));
        // The one album that matches: Enter adds to it.
        type(page, "pe", true);
        return waitFor(function () {
            return page.log.sent.length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(sent(page), ["/albums/2/items {\"add\":[1]}"]);
        // No album of that name: Enter makes it.
        type(page, "Holiday", true);
        return waitFor(function () {
            return page.log.sent.length === 2;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/albums", {name: "Holiday", kind: "manual",
                                                              ids: [1]}]);
        return settle(20);
    }).then(function () {
        // The focus is back in the box after a change made with Enter.
        assert.ok(page.document.activeElement.classes.indexOf("album-picker-find") >= 0);
    });
});

test("a click on an album the picture is in takes it out; the focus stays on that row",
     function () {
         var page = serverPage();
         return loaded(page).then(function () {
             return picker(page, [1]);
         }).then(function () {
             var row = dialog(page).querySelector('[data-album="1"]');
             assert.strictEqual(row.getAttribute("aria-pressed"), "true");
             row.focus();
             row.click();
             return waitFor(function () {
                 return page.log.sent.length === 1 && !dialog(page).querySelector(
                     ".album-picker-item").disabled;
             });
         }).then(function () {
             assert.deepStrictEqual(sent(page), ["/albums/1/items {\"remove\":[1]}"]);
             var focused = page.document.activeElement;
             assert.strictEqual(focused.getAttribute("data-album"), "1");
             assert.strictEqual(focused.getAttribute("aria-pressed"), "false");
             assert.ok(dialog(page).contains(focused));
         });
     });

test("the keys of the marks go by the record", function () {
    var page = serverPage();
    var VIEWER = null;
    var rec = null;
    var marked = 0;
    var context = {get: function () {
        return null;
    }, onMarked: function () {
        marked += 1;
    }};
    return loaded(page).then(function () {
        VIEWER = page.context.VIEWER;
        rec = page.data.records[2];             // rated 3 stars
        assert.ok(VIEWER.marks.byKey(rec, context, "3"));
        assert.ok(VIEWER.marks.byKey(rec, context, "x") === false);
        return settle(20);
    }).then(function () {
        assert.deepStrictEqual(sent(page), []);     // (3 again: nothing to change)
        VIEWER.marks.byKey(rec, context, "F");
        return waitFor(function () {
            return marked === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(sent(page), ["/marks {\"ids\":[3],\"favorite\":true}"]);
        assert.strictEqual(rec.fav, 1);
        VIEWER.marks.byKey(rec, context, "0");
        return waitFor(function () {
            return marked === 2;
        });
    }).then(function () {
        assert.strictEqual(page.log.sent[1][1].rating, 0);
        assert.strictEqual(rec.rt, undefined);
        VIEWER.marks.byKey(rec, context, "0");      // (no rating left: nothing)
        return settle(20);
    }).then(function () {
        assert.strictEqual(page.log.sent.length, 2);
    });
});

test("a star pressed in the information pane keeps the focus", function () {
    var page = serverPage();
    var box = null;
    var context = null;
    var pressed = null;
    var rec = null;
    return loaded(page).then(function () {
        var VIEWER = page.context.VIEWER;
        rec = page.data.records[1];
        box = page.document.createElement("div");
        page.document.body.appendChild(box);
        context = {get: function () {
            return null;
        }, onMarked: function () {
            VIEWER.renderInfo(box, rec, null, context);     // (drawn again, as pages do)
        }};
        VIEWER.renderInfo(box, rec, null, context);
        pressed = box.querySelector('.info-mark-star[data-stars="2"]');
        pressed.focus();
        pressed.click();
        return waitFor(function () {
            return rec.rt === 2;
        });
    }).then(function () {
        return settle(5);
    }).then(function () {
        var focused = page.document.activeElement;
        assert.notStrictEqual(focused, pressed);            // (a new button)
        assert.ok(box.contains(focused));
        assert.strictEqual(focused.getAttribute("data-stars"), "2");
        assert.strictEqual(focused.getAttribute("aria-pressed"), "true");
        // The favorite button too.
        box.querySelector(".info-mark-favorite").focus();
        box.querySelector(".info-mark-favorite").click();
        return waitFor(function () {
            return rec.fav === 1;
        });
    }).then(function () {
        return settle(5);
    }).then(function () {
        var focused = page.document.activeElement;
        assert.ok(focused.classes.indexOf("info-mark-favorite") >= 0);
        assert.strictEqual(focused.getAttribute("aria-pressed"), "true");
    });
});

// A button's label (the long one: phones show a shorter one).
function longLabel(node) {
    var long = node.querySelector(".wide-only");
    assert.ok(long, "no long label");
    assert.strictEqual(node.getAttribute("aria-label"), long.textContent);
    return long.textContent;
}

test("Save as album: not for a visibility switch alone", function () {
    var page = serverPage("?junk=1");
    return loaded(page).then(function () {
        assert.strictEqual(byId(page, "save-search").hidden, true);
        search(page, "beach");
        return waitFor(function () {
            return !byId(page, "save-search").hidden;
        });
    }).then(function () {
        assert.strictEqual(longLabel(byId(page, "save-search")), "Save as album");
        // (Phones show a short one: the results head is one line there.)
        assert.strictEqual(byId(page, "save-search").querySelector(".narrow-only").textContent,
                           "Save");
    });
});

test("Save for the smart album whose search was opened updates it", function () {
    var page = serverPage("?q=beach+OR+sunset&album=12");
    return loaded(page).then(function () {
        var button = byId(page, "save-search");
        assert.strictEqual(button.hidden, false);
        assert.strictEqual(longLabel(button), "Save the album\u2019s search");
        button.click();
        return settle(5);
    }).then(function () {
        var box = dialog(page);
        var name = box.querySelector("input");
        assert.strictEqual(name.value, "Water");
        var labels = box.querySelectorAll(".modal-foot button").map(function (node) {
            return node.textContent;
        });
        assert.deepStrictEqual(labels, ["Cancel", "Save as a new album", "Update"]);
        // A new album may not take the same name.
        labels = box.querySelectorAll(".modal-foot button");
        labels[1].click();
        assert.deepStrictEqual(sent(page), []);
        assert.ok(/another name/.test(box.textContent), box.textContent);
        labels[2].click();
        return waitFor(function () {
            return page.log.sent.length === 1;
        });
    }).then(function () {
        var entry = page.log.sent[0];
        assert.strictEqual(entry[0], "/albums/12");
        assert.strictEqual(entry[1].query, "beach OR sunset");
        assert.strictEqual(entry[1].name, undefined);       // (the name kept)
        return waitFor(function () {
            return !dialog(page);
        });
    }).then(function () {
        var toasts = byId(page, "shell-toasts");
        assert.ok(/Updated the smart album Water/.test(toasts ? toasts.textContent : ""));
        // Still the album's search: Save offers to update it again.
        assert.strictEqual(longLabel(byId(page, "save-search")), "Save the album\u2019s search");
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
