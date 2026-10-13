/* Tests for the GPS check in the pages (static/viewer.js, search-core.js,
   site.js): the admin's note in the information pane on a picture whose
   position was set aside ("Location ignored: ...", the place it gave,
   "Use this location anyway"), "Ignore location" among the admin actions,
   what the route answers changing the record in place, nothing of it for
   visitors, is:gps-ignored, and an estimated place's label; the other
   records a choice changes, where the focus goes, a pane that shows
   another picture by the time the answer comes, and the Manage page's
   count (static/place-names.js). The real page scripts on the made-up DOM
   of tests/js/app_meaning_test.js, with a fake fetch() for the server's
   answers. Every place here is made up.

   Run by tests/test_gps_check.py, or directly:
       node tests/js/gps_check_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var settle = FAKE.settle;

var STATIC = path.join(__dirname, "..", "..", "static");
var SearchCore = require(path.join(STATIC, "search-core.js"));
var VIEWER = require(path.join(STATIC, "viewer.js"));

var REASON = "4 pictures taken around the same time are far away: one taken 2 minutes " +
    "later is 15,000 km away";

function records() {
    return [
        {i: 1, l: "lib", t: "Harbour", g: ["harbour"], ds: 1, dk: "2021-03-04", gp: 1,
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 1},
        {i: 2, l: "lib", t: "Sign at the beach", g: ["sign"], ds: 1, dk: "2021-03-04", gx: 1}
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

/* The search page on the server (admin: the admin's); page.log.sent: [[route,
   body]] of the admin requests made. */
function serverPage(admin) {
    var log = {sent: []};
    var data = {records: records()};
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        var node = document.createElement(tag);
        node.namespaceURI = ns;
        return node;
    };
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: admin, static: false}};
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
        var plain = String(url).split("?")[0];
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: data.records});
        }
        var route = /^\/api\/admin(\/.*)$/.exec(plain);
        if (!route) {
            return response(404, {error: "not here"});
        }
        log.sent.push([route[1], body]);
        if (route[1] === "/places") {
            return response(200, log.places || {});
        }
        if (/^\/images\/\d+\/location$/.test(route[1]) && log.location) {
            return log.location(body);
        }
        if (/^\/images\/\d+\/location$/.test(route[1])) {
            return response(200, body.use ? {
                id: 2, record: {gp: 1, ci: "Farhaven", rg: "Midlands", co: "Testland",
                                cc: "XB", km: 0},
                location_check: {ignored: false, reason: REASON, choice: "use", place: "",
                                 estimated: false}
            } : {
                id: 2, record: {gx: 1},
                location_check: {ignored: true, reason: REASON, choice: "", place:
                                 "Farhaven, Midlands, Testland", estimated: false}
            });
        }
        return response(404, {error: "not here"});
    }
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
        return /\d+ pictures?/.test(FAKE.byId(page, "result-count").textContent);
    });
}

function buttons(box) {
    return box.querySelectorAll("button").map(function (button) {
        return button.textContent;
    });
}

function button(box, text) {
    return box.querySelectorAll("button").filter(function (node) {
        return node.textContent === text;
    })[0];
}

// The information pane of a record with a detail, as the pages draw it.
function pane(page, rec, detail) {
    var box = page.document.createElement("div");
    page.document.body.appendChild(box);
    var state = {edited: 0, detail: detail};
    var context = {get: function (id) {
        return page.data.records.filter(function (other) {
            return other.i === id;
        })[0] || null;
    }, onEdited: function (id, shown) {
        state.edited += 1;
        state.detail = shown;
    }};
    page.context.VIEWER.renderInfo(box, rec, detail, context);
    return {box: box, state: state, context: context};
}

function toasts(page) {
    var region = page.document.getElementById("shell-toasts");
    return region ? region.textContent : "";
}

var IGNORED = {id: 2, location_check: {ignored: true, reason: REASON, choice: "",
                                       place: "Farhaven, Midlands, Testland", estimated: false}};
var USED = {gp: 1, ci: "Farhaven", rg: "Midlands", co: "Testland", cc: "XB", km: 0};

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the admin sees why a position was set aside, and can use it anyway", function () {
    var page = serverPage(true);
    var drawn = null;
    var rec = null;
    return loaded(page).then(function () {
        rec = page.data.records[1];
        drawn = pane(page, rec, {id: 2, location_check: {
            ignored: true, reason: REASON, choice: "", place: "Farhaven, Midlands, Testland",
            estimated: false}});
        var note = drawn.box.querySelector(".info-location-check");
        assert.ok(note, "no note");
        assert.ok(note.classes.indexOf("is-ignored") >= 0);
        assert.strictEqual(note.querySelector(".info-location-why").textContent,
                           "Location ignored: " + REASON + ".");
        assert.ok(/Ignored place: Farhaven, Midlands, Testland/.test(note.textContent));
        assert.deepStrictEqual(buttons(note), ["Use this location anyway"]);
        // Set aside: no "Ignore location" among the admin actions.
        assert.strictEqual(button(drawn.box, "Ignore location"), undefined);
        button(note, "Use this location anyway").click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent, [["/images/2/location", {use: true}]]);
        assert.strictEqual(rec.gp, 1);
        assert.strictEqual(rec.ci, "Farhaven");
        assert.strictEqual(rec.gx, undefined);
        assert.strictEqual(drawn.state.detail.location_check.choice, "use");
        // Drawn again: the place, the owner's choice and "Ignore location".
        var note = drawn.box.querySelector(".info-location-check");
        assert.ok(note.classes.indexOf("is-ignored") < 0);
        assert.strictEqual(note.textContent, "Location used as you chose; the GPS check " +
                           "would ignore it: " + REASON + ".");
        assert.ok(/Farhaven, Midlands, Testland \(under 1 km\)/.test(drawn.box.textContent),
                  drawn.box.textContent);
        button(drawn.box, "Ignore location").click();
        return waitFor(function () {
            return drawn.state.edited === 2;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/images/2/location", {use: false}]);
        assert.strictEqual(rec.gx, 1);
        assert.strictEqual(rec.gp, undefined);
        assert.strictEqual(rec.ci, undefined);
        assert.ok(drawn.box.querySelector(".info-location-check.is-ignored"));
        return settle(5);
    });
});

test("a located picture offers Ignore location; one without a position nothing", function () {
    var page = serverPage(true);
    return loaded(page).then(function () {
        var rec = page.data.records[0];
        var drawn = pane(page, rec, {id: 1, location_check: {
            ignored: false, reason: "", choice: "", place: "", estimated: false}});
        assert.strictEqual(drawn.box.querySelector(".info-location-check"), null);
        assert.ok(button(drawn.box, "Ignore location"));
        drawn = pane(page, {i: 3, l: "lib", t: "No position"}, {id: 3, location_check: null});
        assert.strictEqual(button(drawn.box, "Ignore location"), undefined);
        assert.strictEqual(drawn.box.querySelector(".info-location-check"), null);
    });
});

test("visitors see nothing of it", function () {
    var page = serverPage(false);
    return loaded(page).then(function () {
        var drawn = pane(page, {i: 2, l: "lib", t: "Sign"}, {id: 2, location_check: {
            ignored: true, reason: REASON, choice: "", place: "Farhaven", estimated: false}});
        assert.strictEqual(drawn.box.querySelector(".info-location-check"), null);
        assert.strictEqual(button(drawn.box, "Use this location anyway"), undefined);
        assert.ok(!/Farhaven|ignored/i.test(drawn.box.textContent), drawn.box.textContent);
    });
});

test("is:gps-ignored finds the positions set aside", function () {
    var list = records().concat([{i: 3, l: "lib", t: "Other"}]);
    ["is:gps-ignored", "is:gps_ignored", "is:location-ignored"].forEach(function (query) {
        var found = SearchCore.search(list, query, {}).items.map(function (item) {
            return item.i !== undefined ? item.i : item.rec ? item.rec.i : item;
        });
        assert.deepStrictEqual(found, [2], query);
    });
    var others = SearchCore.search(list, "-is:gps-ignored", {}).items.length;
    assert.strictEqual(others, 2);
});

test("an estimated place says so", function () {
    var rec = {i: 4, ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", pe: 1};
    assert.strictEqual(VIEWER.format.placeText(rec), "Coralport, North Coast, Examplia " +
                       "(estimated from pictures taken around the same time)");
    rec = {i: 5, ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 3, gp: 1};
    assert.strictEqual(VIEWER.format.placeText(rec), "Coralport, North Coast, Examplia (3 km)");
});

test("a choice changes the other records the check changed, and keeps the focus", function () {
    var page = serverPage(true);
    var drawn = null;
    page.log.location = function () {
        return response(200, {id: 2, record: USED, copies: 1, more: 0,
                              location_check: {ignored: false, reason: REASON, choice: "use",
                                               place: "", estimated: false},
                              others: {"1": {gx: 1}, "7": USED}});
    };
    return loaded(page).then(function () {
        drawn = pane(page, page.data.records[1], IGNORED);
        var use = button(drawn.box, "Use this location anyway");
        use.focus();
        use.click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        // The phone picture the check now sets aside lost its place.
        var phone = page.data.records[0];
        assert.strictEqual(phone.gx, 1);
        assert.strictEqual(phone.gp, undefined);
        assert.strictEqual(phone.ci, undefined);
        assert.strictEqual(page.data.records[1].ci, "Farhaven");
        var said = toasts(page);
        assert.ok(/The same goes for 1 copy of it\./.test(said), said);
        assert.ok(/The check changed 1 other picture too\./.test(said), said);
        // The focus is on the button that undoes it, not lost to the page.
        assert.strictEqual(page.document.activeElement.textContent, "Ignore location");
        return settle(5);
    });
});

test("an answer that comes after the pane moved on does not draw over it", function () {
    var page = serverPage(true);
    var drawn = null;
    var release = null;
    page.log.location = function () {
        return new Promise(function (resolve) {
            release = function () {
                resolve(response(200, {id: 2, record: USED, copies: 0, more: 0, others: {},
                                       location_check: {ignored: false, reason: REASON,
                                                        choice: "use", place: "",
                                                        estimated: false}}));
            };
        });
    };
    return loaded(page).then(function () {
        drawn = pane(page, page.data.records[1], IGNORED);
        button(drawn.box, "Use this location anyway").click();
        return waitFor(function () {
            return release !== null;
        });
    }).then(function () {
        // Another picture is shown in the pane by now.
        page.context.VIEWER.renderInfo(drawn.box, page.data.records[0], null, drawn.context);
        release();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        assert.strictEqual(drawn.box.querySelector(".info-title").textContent, "Harbour");
        assert.strictEqual(drawn.box.querySelector(".info-location-check"), null);
        assert.strictEqual(page.data.records[1].ci, "Farhaven");
        return settle(5);
    });
});

test("a position ignored takes its local time with it (local_times.after_check)", function () {
    var page = serverPage(true);
    var drawn = null;
    var rec = null;
    page.log.location = function () {
        // (Its time is the camera's again; another picture's too.)
        return response(200, {id: 1, record: {gx: 1, dk: "2021-03-04",
                                              ts: "2021-03-04T08:00:10"},
                              time: {taken_at: "2021-03-04T08:00:10", tz_offset: "",
                                     clock: null},
                              copies: 0, more: 0,
                              location_check: {ignored: true, reason: "", choice: "ignore",
                                               place: "Coralport", estimated: false},
                              others: {"2": {gx: 1, dk: "2021-03-03",
                                             ts: "2021-03-03T23:50:00"}}});
    };
    return loaded(page).then(function () {
        rec = page.data.records[0];
        rec.ts = "2021-03-04T15:00:10";
        drawn = pane(page, rec, {id: 1, taken_at: "2021-03-04T15:00:10", tz_offset: "+09:00",
                                 clock: {taken_at: "2021-03-04T08:00:10", tz_offset: "",
                                         kind: "gps", zone: "Asia/Tokyo"},
                                 location_check: {ignored: false, reason: "", choice: "",
                                                  place: "", estimated: false}});
        assert.ok(/Camera clock/.test(drawn.box.textContent), drawn.box.textContent);
        button(drawn.box, "Ignore location").click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        assert.strictEqual(rec.ts, "2021-03-04T08:00:10");
        assert.strictEqual(rec.gp, undefined);
        assert.strictEqual(page.data.records[1].dk, "2021-03-03");
        assert.strictEqual(page.data.records[1].ts, "2021-03-03T23:50:00");
        assert.strictEqual(drawn.state.detail.taken_at, "2021-03-04T08:00:10");
        assert.strictEqual(drawn.state.detail.clock, null);
        assert.ok(!/Camera clock/.test(drawn.box.textContent), drawn.box.textContent);
        return settle(5);
    });
});

test("the Manage page counts the positions set aside in every state", function () {
    var page = serverPage(true);
    page.log.places = {available: true, cities: 3, built: null, rebuild: false, error: "",
                       pictures: {located: 0, named: 0, region_only: 0, far: 0, pending: 0},
                       gps_ignored: 2, city_km: 25, max_km: 200, job: null,
                       attribution_url: "https://example.invalid/a",
                       license_url: "https://example.invalid/l"};
    var box = page.document.createElement("section");
    box.id = "place-names";
    page.document.body.appendChild(box);
    var code = fs.readFileSync(path.join(STATIC, "place-names.js"), "utf-8");
    return loaded(page).then(function () {
        vm.runInContext(code, page.context, {filename: "place-names.js"});
        return waitFor(function () {
            return /GPS check/.test(box.textContent);
        });
    }).then(function () {
        var status = box.querySelector(".place-names-status").textContent;
        assert.ok(status.indexOf("No picture has a GPS position that is used. The GPS check " +
                                 "ignored the position of 2 pictures (search " +
                                 "is:gps-ignored).") >= 0, status);
        return settle(5);
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
