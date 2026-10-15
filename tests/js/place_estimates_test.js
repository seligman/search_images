/* Tests for estimated places in the pages (static/viewer.js, search-core.js,
   map.js, export-review.js, place-names.js; see place_estimates.py): the
   place line of a picture whose place is estimated ("estimated", and why on
   a tap), the admin's Accept / Reject / Change place... and Set place...
   (the town search), what the route answers changing the record in place,
   nothing of the choices for visitors, a place its file names (the
   picture's own), is:place-estimated, hollow pins on the map, the export's
   "Estimated places" and the Manage page's count.
   The real page scripts on the made-up DOM of tests/js/app_meaning_test.js,
   with a fake fetch() for the server's answers. Every place here is made up.

   Run by tests/test_place_estimates.py, or directly:
       node tests/js/place_estimates_test.js
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
var MAP = require(path.join(STATIC, "map.js"));
var REVIEW = require(path.join(STATIC, "export-review.js"));

var REASON = "estimated from pictures taken around the same time: one taken 4 minutes " +
    "earlier, one 2 minutes later, 310 m apart";

function records() {
    return [
        {i: 1, l: "lib", t: "Harbour", g: ["harbour"], ds: 1, dk: "2021-03-04", gp: 1,
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 1},
        {i: 2, l: "lib", t: "Boat", g: ["boat"], ds: 1, dk: "2021-03-04",
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", pe: 1, ps: "t"},
        {i: 3, l: "lib", t: "Wedding", g: ["wedding"], ds: 1,
         rg: "North Coast", co: "Examplia", cc: "XA", pe: 1, ps: "a"},
        {i: 4, l: "lib", t: "Shell", g: ["shell"], ds: 1, dk: "2021-03-04"}
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
   body]] of the admin requests made (a GET's query in the route). */
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
        log.sent.push([route[1] === "/places/towns" ? String(url).replace("/api/admin", "")
            : route[1], body]);
        if (route[1] === "/places") {
            return response(200, log.places || {});
        }
        if (route[1] === "/places/towns") {
            return response(200, {available: true, position: null, towns: [
                {name: "Coralport", region: "North Coast", country: "Examplia", cc: "XA",
                 lat: 5.0, lon: -150.0, population: 12000}]});
        }
        if (/^\/images\/\d+\/place$/.test(route[1])) {
            if (body.choice === "accept" || body.choice === "set") {
                return response(200, {id: 2, record: {ci: "Coralport", rg: "North Coast",
                                                      co: "Examplia", cc: "XA", ps: "o"},
                                      place: {city: "Coralport", region: "North Coast",
                                              country: "Examplia", cc: "XA",
                                              source: "owner"},
                                      place_estimate: {source: "owner", reason: "set by you",
                                                       radius_km: null, choice: "set",
                                                       place: "Coralport, North Coast, " +
                                                           "Examplia", lat: 5, lon: -150,
                                                       ids: []},
                                      // (Another picture of its visit took a named place.)
                                      others: {1: {gp: 1, ci: "Coralport", rg: "North Coast",
                                                   co: "Examplia", cc: "XA", km: 1,
                                                   np: "Example Museum", nk: "museum",
                                                   na: 1}},
                                      more: 0});
            }
            if (body.choice === "clear" && log.cleared) {
                return response(200, log.cleared);
            }
            if (body.choice === "reject") {
                return response(200, {id: 2, record: {}, place: null, place_estimate: {
                    source: "", reason: "you rejected the estimate", radius_km: null,
                    place: "", choice: "reject", lat: null, lon: null, ids: []}});
            }
            return response(200, {id: 2, record: {ci: "Coralport", rg: "North Coast",
                                                  co: "Examplia", cc: "XA", pe: 1, ps: "t"},
                                  place: null, place_estimate: ESTIMATE});
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

var ESTIMATE = {source: "time", reason: REASON, radius_km: 0.42,
                place: "Coralport, North Coast, Examplia", lat: 5.0, lon: -150.0,
                ids: [1, 9], choice: ""};

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

var ELLIPSIS = String.fromCharCode(0x2026);

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the place line says where an estimate comes from", function () {
    var place = {ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA"};
    function rec(extra) {
        var result = {};
        Object.keys(place).forEach(function (key) {
            result[key] = place[key];
        });
        Object.keys(extra).forEach(function (key) {
            result[key] = extra[key];
        });
        return result;
    }
    var text = VIEWER.format.placeText;
    assert.strictEqual(text(rec({pe: 1, ps: "t"})), "Coralport, North Coast, Examplia " +
                       "(estimated from pictures taken around the same time)");
    assert.strictEqual(text(rec({pe: 1, ps: "c"})), "Coralport, North Coast, Examplia " +
                       "(estimated from a copy of this picture)");
    assert.strictEqual(text({rg: "North Coast", co: "Examplia", pe: 1, ps: "a"}),
                       "North Coast, Examplia (estimated from the library)");
    // (An older record without ps.)
    assert.strictEqual(text(rec({pe: 1})), "Coralport, North Coast, Examplia " +
                       "(estimated from pictures taken around the same time)");
    // The owner's own place: plain to visitors, "set by you" to the admin.
    assert.strictEqual(text(rec({ps: "o"})), "Coralport, North Coast, Examplia");
    assert.strictEqual(text(rec({ps: "o"}), true), "Coralport, North Coast, Examplia " +
                       "(set by you)");
    var reason = VIEWER.format.estimateReason;
    assert.strictEqual(reason(rec({pe: 1, ps: "t"}), ESTIMATE),
                       "Estimated from pictures taken around the same time: one taken 4 " +
                       "minutes earlier, one 2 minutes later, 310 m apart. It may be off by " +
                       "about 400 m.");
    assert.strictEqual(reason(rec({pe: 1, ps: "a"}), null), "Estimated from the library.");
    assert.strictEqual(reason(rec({pe: 1, ps: "t"}), {reason: "x", radius_km: 2.345}),
                       "X. It may be off by about 2.3 km.");
});

test("a place estimated from a video's GPS track says so", function () {
    var page = serverPage(false);
    return loaded(page).then(function () {
        var rec = page.data.records[1];
        var reason = "estimated from the GPS track of a video recorded around the same " +
            "time: 2 points of the track before it, the nearest at the same time, 320 m apart";
        var drawn = pane(page, rec, {id: 2, lat: null, lon: null,
                                     place_estimate: {source: "time", track: "all",
                                                      reason: reason, radius_km: 0.4,
                                                      place: "Coralport"}});
        var toggle = drawn.box.querySelector(".info-place-estimated");
        assert.strictEqual(toggle.textContent, "estimated from a video's GPS track");
        assert.strictEqual(drawn.box.querySelector(".info-place-reason").textContent,
                           "Estimated from the GPS track of a video recorded around the " +
                           "same time: 2 points of the track before it, the nearest at the " +
                           "same time, 320 m apart. It may be off by about 400 m.");
        // Pictures and a track together: pictures, as before.
        drawn = pane(page, rec, {id: 2, lat: null, lon: null,
                                 place_estimate: {source: "time", track: "some",
                                                  reason: REASON, radius_km: 0.4}});
        assert.strictEqual(drawn.box.querySelector(".info-place-estimated").textContent,
                           "estimated from pictures taken around the same time");
    });
});

test("is:place-estimated, and place: finds estimated places too", function () {
    function ids(query) {
        return SearchCore.search(records(), query, {}).items.map(function (item) {
            return item.i !== undefined ? item.i : item.rec ? item.rec.i : item;
        }).sort();
    }
    ["is:place-estimated", "is:estimated", "is:estimated-place"].forEach(function (query) {
        assert.deepStrictEqual(ids(query), [2, 3], query);
    });
    assert.deepStrictEqual(ids("-is:place-estimated"), [1, 4]);
    assert.deepStrictEqual(ids("place:coralport"), [1, 2]);
    assert.deepStrictEqual(ids("place:\"north coast\""), [1, 2, 3]);
    assert.deepStrictEqual(ids("has:gps"), [1]);
});

test("visitors see where it comes from, with no choices", function () {
    var page = serverPage(false);
    return loaded(page).then(function () {
        var rec = page.data.records[1];
        var drawn = pane(page, rec, {id: 2, lat: null, lon: null,
                                     place_estimate: {source: "time", reason: REASON,
                                                      radius_km: 0.42,
                                                      place: "Coralport"}});
        var place = drawn.box.querySelector(".info-place");
        assert.ok(place, "no place");
        assert.strictEqual(place.querySelector(".info-chip").textContent,
                           "Coralport, North Coast, Examplia");
        var toggle = place.querySelector(".info-place-estimated");
        var why = place.querySelector(".info-place-reason");
        assert.strictEqual(toggle.textContent, "estimated from pictures taken around the " +
                           "same time");
        assert.strictEqual(toggle.getAttribute("aria-expanded"), "false");
        assert.strictEqual(toggle.getAttribute("aria-controls"), why.id);
        assert.ok(why.hidden);
        assert.ok(/310 m apart\. It may be off by about 400 m\./.test(toggle.title));
        toggle.click();
        assert.ok(!why.hidden);
        assert.strictEqual(toggle.getAttribute("aria-expanded"), "true");
        toggle.click();
        assert.ok(why.hidden);
        assert.strictEqual(drawn.box.querySelector(".info-place-estimate"), null);
        assert.strictEqual(button(drawn.box, "Set place\u2026"), undefined);
        // The owner's own place is plain to them.
        drawn = pane(page, {i: 5, l: "lib", t: "Own", ci: "Coralport", co: "Examplia",
                            ps: "o"}, {id: 5, lat: null, lon: null});
        assert.ok(!/set by you|estimated/.test(drawn.box.textContent), drawn.box.textContent);
    });
});

test("the admin accepts an estimate", function () {
    var page = serverPage(true);
    var drawn = null;
    var rec = null;
    return loaded(page).then(function () {
        rec = page.data.records[1];
        drawn = pane(page, rec, {id: 2, lat: null, lon: null, place_estimate: ESTIMATE});
        var note = drawn.box.querySelector(".info-place-estimate");
        assert.ok(note, "no note");
        assert.deepStrictEqual(buttons(note), ["Accept", "Reject", "Change place\u2026"]);
        // ("Set place..." only for a picture without a place.)
        assert.strictEqual(drawn.box.querySelectorAll(".info-place-set").length, 1);
        var accept = button(note, "Accept");
        accept.focus();
        page.scrolled = [];
        page.realScroll = page.document.body.constructor.prototype.scrollIntoView;
        page.document.body.constructor.prototype.scrollIntoView = function (options) {
            page.scrolled.push([this.textContent, options && options.block]);
        };
        accept.click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        page.document.body.constructor.prototype.scrollIntoView = page.realScroll;
        assert.deepStrictEqual(page.log.sent, [["/images/2/place", {choice: "accept"}]]);
        // The focus on the first choice now, in the middle of what scrolls:
        // not under the toast at the foot of the screen that says it is done.
        assert.strictEqual(page.document.activeElement.textContent, "Clear");
        assert.deepStrictEqual(page.scrolled, [["Clear", "center"]]);
        assert.strictEqual(rec.ps, "o");
        assert.strictEqual(rec.pe, undefined);
        // The other record the answer has changes in place too.
        var other = page.data.records[0];
        assert.deepStrictEqual([other.np, other.nk, other.na, other.ci, other.gp],
                               ["Example Museum", "museum", 1, "Coralport", 1]);
        assert.strictEqual(drawn.state.detail.place_estimate.choice, "set");
        var note = drawn.box.querySelector(".info-place-estimate");
        assert.ok(/You set this place/.test(note.textContent), note.textContent);
        assert.deepStrictEqual(buttons(note), ["Clear", "Change place\u2026"]);
        assert.ok(/Coralport, North Coast, Examplia/.test(drawn.box.textContent));
        assert.ok(/set by you/.test(drawn.box.querySelector(".info-place").textContent));
        return settle(5);
    });
});

test("the admin rejects an estimate, and sets a place from the town search", function () {
    var page = serverPage(true);
    var drawn = null;
    var rec = null;
    var form = null;
    return loaded(page).then(function () {
        rec = page.data.records[1];
        drawn = pane(page, rec, {id: 2, lat: null, lon: null, place_estimate: ESTIMATE});
        button(drawn.box.querySelector(".info-place-estimate"), "Reject").click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0], ["/images/2/place", {choice: "reject"}]);
        assert.strictEqual(rec.co, undefined);
        assert.strictEqual(rec.pe, undefined);
        var note = drawn.box.querySelector(".info-place-estimate");
        assert.ok(/You rejected the estimated place/.test(note.textContent));
        assert.deepStrictEqual(buttons(note), ["Estimate again", "Set place\u2026"]);
        // (Rejected: no second "Set place..." among the actions.)
        assert.strictEqual(drawn.box.querySelectorAll(".info-place-set").length, 1);
        button(note, "Set place\u2026").click();
        form = drawn.box.querySelector(".info-place-form");
        assert.ok(form, "no form");
        var input = form.querySelector("input");
        input.value = "cora";
        input.dispatchEvent({type: "input", target: input});
        return waitFor(function () {
            return form.querySelectorAll(".info-place-choice").length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/places/towns?q=cora", null]);
        var choice = form.querySelector(".info-place-choice");
        assert.strictEqual(choice.textContent, "Coralport, North Coast, Examplia");
        choice.click();
        return waitFor(function () {
            return drawn.state.edited === 2;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[2], ["/images/2/place",
                                                  {choice: "set", lat: 5, lon: -150}]);
        assert.strictEqual(rec.ps, "o");
        assert.strictEqual(rec.ci, "Coralport");
        return settle(5);
    });
});

test("a picture without a place offers Set place... to the admin only", function () {
    var page = serverPage(true);
    return loaded(page).then(function () {
        var rec = page.data.records[3];
        var drawn = pane(page, rec, {id: 4, lat: null, lon: null, place_estimate: null});
        assert.strictEqual(drawn.box.querySelector(".info-place-estimate"), null);
        var set = button(drawn.box, "Set place\u2026");
        assert.ok(set, buttons(drawn.box).join(", "));
        var scrolled = [];
        var proto = page.document.body.constructor.prototype;
        var realScroll = proto.scrollIntoView;
        proto.scrollIntoView = function (options) {
            scrolled.push([this.className, options && options.block]);
        };
        set.click();
        proto.scrollIntoView = realScroll;
        var form = drawn.box.querySelector(".info-place-form");
        assert.ok(form);
        assert.ok(set.hidden);
        // In sight (no touch screen here: only as far as it takes).
        assert.deepStrictEqual(scrolled, [["info-place-form", "nearest"]]);
        // Esc closes it (and does not reach the viewer).
        var stopped = false;
        form.dispatchEvent({type: "keydown", key: "Escape", target: form.querySelector("input"),
                            preventDefault: function () {},
                            stopPropagation: function () {
                                stopped = true;
                            }});
        assert.ok(stopped);
        assert.strictEqual(drawn.box.querySelector(".info-place-form"), null);
        assert.ok(!set.hidden);
        // A located picture: nothing to set.
        drawn = pane(page, page.data.records[0], {id: 1, lat: 5, lon: -150,
                                                  location_check: null});
        assert.strictEqual(button(drawn.box, "Set place\u2026"), undefined);
    });
});

test("a located picture whose file names its place: those names, its pin its position",
     function () {
    // (place_estimates.gps_file_columns: no distance, no note, nothing to choose.)
    var named = {i: 8, l: "lib", t: "Village", ds: 1, gp: 1, ci: "Little Ford",
                 rg: "Midlands", co: "Testland", cc: "XB", ps: "f"};
    var detail = {id: 8, lat: 5, lon: -150,
                  exif: {XMPPlace: {city: "Little Ford", region: "Midlands",
                                    country: "Testland", cc: "XB"}},
                  place: {city: "Little Ford", region: "Midlands", country: "Testland",
                          cc: "XB", source: "file"},
                  place_estimate: null, location_check: null};
    assert.strictEqual(VIEWER.format.placeText(named), "Little Ford, Midlands, Testland");
    var owner = serverPage(true);
    return loaded(owner).then(function () {
        var drawn = pane(owner, named, detail);
        var chip = drawn.box.querySelectorAll(".info-chip").filter(function (node) {
            return node.textContent === "Little Ford, Midlands, Testland";
        })[0];
        assert.ok(chip, "no place chip");
        assert.strictEqual(chip.title, "The place its file names (the pin is its GPS " +
                           "position). Show all pictures from Little Ford");
        assert.strictEqual(drawn.box.querySelector(".info-place-estimate"), null);
        assert.strictEqual(button(drawn.box, "Set place" + ELLIPSIS), undefined);
    });
});

test("a place its file names is the picture's own: the admin may change it or say none",
     function () {
    var named = {i: 6, l: "lib", t: "Named", ds: 1, rg: "North Coast", co: "Examplia",
                 cc: "XA", ps: "f"};
    var fileEstimate = {source: "file", reason: "named in its file", radius_km: null,
                        place: "North Coast, Examplia"};
    var detail = {id: 6, lat: null, lon: null,
                  exif: {XMPPlace: {region: "North Coast", country: "Examplia", cc: "XA"}},
                  place: {city: "", region: "North Coast", country: "Examplia", cc: "XA",
                          source: "file"},
                  place_estimate: fileEstimate};
    // Plain, like a place of its own (no "estimated", no distance), to everyone.
    assert.strictEqual(VIEWER.format.placeText(named), "North Coast, Examplia");
    assert.strictEqual(VIEWER.format.placeText(named, true), "North Coast, Examplia");
    var visitor = serverPage(false);
    var owner = serverPage(true);
    var drawn = null;
    return loaded(visitor).then(function () {
        var seen = pane(visitor, named, detail);
        assert.ok(/North Coast, Examplia/.test(seen.box.textContent));
        assert.ok(!/estimated|file names/.test(seen.box.textContent), seen.box.textContent);
        assert.strictEqual(seen.box.querySelector(".info-place-estimate"), null);
        // Its chip says where the place comes from: not a town near a position.
        var chip = seen.box.querySelectorAll(".info-chip").filter(function (node) {
            return node.textContent === "North Coast, Examplia";
        })[0];
        assert.ok(chip, "no place chip");
        assert.strictEqual(chip.title, "The place its file names (no position, so no pin on " +
                           "the map). Show all pictures from North Coast");
        return loaded(owner);
    }).then(function () {
        drawn = pane(owner, named, detail);
        var note = drawn.box.querySelector(".info-place-estimate");
        assert.ok(note, "no note");
        assert.ok(/Its file names this place, without a position/.test(note.textContent),
                  note.textContent);
        assert.deepStrictEqual(buttons(note), ["No place", "Change place" + ELLIPSIS]);
        // (It has a place: no "Set place..." among the actions.)
        assert.strictEqual(button(drawn.box, "Set place" + ELLIPSIS), undefined);
        // "No place": the owner's reject, kept across scans...
        owner.log.cleared = {id: 6, record: {rg: "North Coast", co: "Examplia", cc: "XA",
                                             ps: "f"},
                             place: detail.place, place_estimate: fileEstimate};
        button(note, "No place").click();
        return waitFor(function () {
            return drawn.state.edited === 1;
        }).then(function () {
            assert.deepStrictEqual(owner.log.sent[0], ["/images/6/place", {choice: "reject"}]);
            assert.strictEqual(named.co, undefined);
            assert.strictEqual(named.ps, undefined);
            note = drawn.box.querySelector(".info-place-estimate");
            assert.ok(/You said the picture has no place, although its file names one/.test(
                note.textContent), note.textContent);
            assert.deepStrictEqual(buttons(note), ["Use its file's place",
                                                   "Set place" + ELLIPSIS]);
            // ...and the file's place back (the estimates decide again: the file's first).
            button(note, "Use its file's place").click();
            return waitFor(function () {
                return drawn.state.edited === 2;
            });
        });
    }).then(function () {
        assert.deepStrictEqual(owner.log.sent[1], ["/images/6/place", {choice: "clear"}]);
        assert.strictEqual(named.ps, "f");
        assert.strictEqual(named.co, "Examplia");
        var note = drawn.box.querySelector(".info-place-estimate");
        assert.ok(/Its file names this place/.test(note.textContent), note.textContent);
        // A position the GPS check set aside: its file's names do not count (the
        // estimates decide), so a reject is of the estimated place.
        var aside = pane(owner, {i: 7, l: "lib", t: "Aside", ds: 1}, {
            id: 7, lat: null, lon: null, location_check: {ignored: true, reason: "", choice: "",
                                                          place: "", estimated: false},
            exif: {XMPPlace: {country: "Examplia"}},
            place_estimate: {source: "", reason: "you rejected the estimate",
                             radius_km: null, place: "", choice: "reject", lat: null,
                             lon: null, ids: []}});
        note = aside.box.querySelector(".info-place-estimate");
        assert.ok(/You rejected the estimated place/.test(note.textContent), note.textContent);
        return settle(5);
    });
});

test("an open place form survives the pane drawn again", function () {
    var page = serverPage(true);
    var drawn = null;
    var rec = null;
    return loaded(page).then(function () {
        rec = page.data.records[3];
        var detail = {id: 4, lat: null, lon: null, place_estimate: null};
        drawn = pane(page, rec, detail);
        button(drawn.box, "Set place" + ELLIPSIS).click();
        var input = drawn.box.querySelector(".info-place-form input");
        input.value = "cora";
        input.dispatchEvent({type: "input", target: input});
        // (Looks or vectors arrive: the viewer draws the pane again.)
        page.context.VIEWER.renderInfo(drawn.box, rec, detail, drawn.context);
        var again = drawn.box.querySelector(".info-place-form input");
        assert.ok(again, "the form is gone");
        assert.notStrictEqual(again, input);
        assert.strictEqual(again.value, "cora");
        assert.ok(button(drawn.box, "Set place" + ELLIPSIS).hidden);
        return waitFor(function () {
            return drawn.box.querySelectorAll(".info-place-choice").length === 1;
        });
    }).then(function () {
        // Another picture: the form does not follow.
        page.context.VIEWER.renderInfo(drawn.box, page.data.records[0], {id: 1, lat: 5,
                                                                          lon: -150},
                                       drawn.context);
        assert.strictEqual(drawn.box.querySelector(".info-place-form"), null);
        page.context.VIEWER.renderInfo(drawn.box, rec, {id: 4, lat: null, lon: null},
                                       drawn.context);
        assert.strictEqual(drawn.box.querySelector(".info-place-form"), null);
        return settle(5);
    });
});

test("hollow pins on the map", function () {
    var points = [{id: 1, lat: 5, lon: -150}, {id: 2, lat: 5.0001, lon: -150, e: 1},
                  {id: 3, lat: 50, lon: -100, e: 1, r: 0.4}, {id: 4, lat: 50.0001, lon: -100,
                                                               e: 1}];
    var groups = MAP.cluster(points, 1024);
    var byIds = {};
    groups.forEach(function (group) {
        byIds[group.ids.slice().sort().join(",")] = group;
    });
    assert.strictEqual(byIds["1,2"].estimated, 1);
    assert.strictEqual(byIds["3,4"].estimated, 2);
    assert.strictEqual(byIds["3,4"].count, 2);
});

test("the export's Estimated places", function () {
    assert.ok(REVIEW.OPTIONS.estimated_places.label === "Estimated places");
    var options = {location: true, estimated_places: true};
    assert.ok(/location \(GPS position, place names and named place\) and estimated place\./.test(
        REVIEW.detailsText(options)), REVIEW.detailsText(options));
    options.location = false;
    assert.ok(!/estimated/.test(REVIEW.detailsText(options)));
});

test("the Manage page counts the estimated places", function () {
    var page = serverPage(true);
    page.log.places = {available: true, cities: 3, built: null, rebuild: false, error: "",
                       pictures: {located: 5, named: 5, region_only: 0, far: 0, pending: 0},
                       gps_ignored: 0, city_km: 25, max_km: 200, job: null,
                       estimates: {estimated: 12, copy: 1, time: 4, area: 7, owner: 2,
                                   file: 3, rejected: 1},
                       attribution_url: "https://example.invalid/a",
                       license_url: "https://example.invalid/l"};
    var box = page.document.createElement("section");
    box.id = "place-names";
    page.document.body.appendChild(box);
    var code = fs.readFileSync(path.join(STATIC, "place-names.js"), "utf-8");
    return loaded(page).then(function () {
        vm.runInContext(code, page.context, {filename: "place-names.js"});
        return waitFor(function () {
            return /estimated place/.test(box.textContent);
        });
    }).then(function () {
        var status = box.querySelector(".place-names-status").textContent;
        assert.ok(status.indexOf("12 pictures without a position have an estimated place: " +
                                 "1 from a copy, 4 from pictures taken around the same time, " +
                                 "7 from the library's area (region only) (search " +
                                 "is:place-estimated). You set the place of 2 pictures. " +
                                 "3 pictures without a position have the place their files " +
                                 "name.") >= 0, status);
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
