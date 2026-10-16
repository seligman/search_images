/* Tests for the People page's "Who's this?" under Node (no packages needed): the real
   page scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake fetch()
   for the server's answers. The list shows how many there are to confirm; the queue
   is walked one at a time with the keys (Enter, N, S or the right arrow, X, U or
   Ctrl+Z, Esc) and the buttons, each item asked about again before it shows (one no
   longer marked, or a person hidden or with other faces meanwhile, is passed over, one
   a grouping moved meanwhile too, with a note); progress (one line of the page, read
   with the question) and the end's tally; Undo one answer after another (a busy server
   or a later change keeps the answers); a press right after an item shows, or a key
   held down, decides nothing, nor leaves; "Group faces again to look for more"; leaving
   draws the list from fresh data; skips are remembered (of this face data); Undo after
   leaving and coming back, or a reload (this tab keeps the answers, of this face data),
   and on the list; the notes about Undo told to screen readers; the list drawn with its
   entry; the keyboard's place; the queue asked for again with the build's number; and
   the phone's layout. All people are made up.

   Run by tests/test_people_queue.py, or directly:
       node tests/js/people_queue_test.js
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

var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");

function read(name) {
    return fs.readFileSync(path.join(STATIC, name), "utf8");
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

var LISTED = [{id: 4, name: "Annabel", label: "Annabel", cover: 41, faces: 6,
               ids: [1, 2, 3, 4, 5, 6]},
              {id: 5, name: "Bo", label: "Bo", cover: 51, faces: 6, ids: [7, 8]},
              {id: 3, name: "", label: "Person 3", cover: 31, faces: 4, ids: [1, 2, 3]}];

// The queue: four faces of nobody and an unnamed person (Person 3), all said to be
// Annabel but the last face (Bo's).
function queueAnswer() {
    return {version: 1, data: "d1", items: [
        {kind: "face", face: 501, to: 4, score: 0.612, lead: 0.3, words: "very like",
         image: 1},
        {kind: "face", face: 502, to: 4, score: 0.55, lead: 0.25, words: "very like",
         image: 9, at: 72.4},
        {kind: "face", face: 503, to: 4, score: 0.5, lead: 0.2, words: "like", image: 2},
        {kind: "face", face: 504, to: 4, score: 0.5, lead: 0.2, words: "like", image: 3},
        {kind: "face", face: 505, to: 5, score: 0.52, lead: 0.2, words: "like", image: 4},
        {kind: "person", person: 3, label: "Person 3", face: 31, faces: 4, show: [31, 32, 33],
         seen: "s3", to: 4, score: 0.6, lead: 0.3, words: "very like"}],
        people: {"4": {id: 4, name: "Annabel", label: "Annabel", faces: 6,
                       show: [41, 42, 43]},
                 "5": {id: 5, name: "Bo", label: "Bo", faces: 6, show: [51, 52]}},
        counts: {faces: 5, persons: 1, person_faces: 4}};
}

// A browser's storage (localStorage, sessionStorage).
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

/* The People page as the admin sees it, at the address's ?... part. server: what the
   fake server does ({stale: face ids no longer marked, moved: face ids a grouping
   moved, building: how many times the queue answers 202 first (with build 7), hidden:
   unnamed people hidden meanwhile, faces: {person: how many faces it has now}, undo:
   [[status, body]] the next undos answer; suggests: the answer whose items the
   suggestions still mark (default: the queue's); local, session: the storage of the
   page opened before in this tab, to open it again}). page.log.sent: [[path, body]] of the
   requests with a body. page.tick(): a second later. */
function peoplePage(searchText, server) {
    server = server || {};
    var log = {requests: [], sent: []};
    var clock = {now: 1000000};
    var grouped = {at: 100};
    var tokens = 0;
    var document = FAKE.makeDocument();
    var config = {mode: "server", page: "people", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false, people: true}};
    FAKE.parseBody(document, fs.readFileSync(path.join(ROOT, "templates", "people.html"),
                                             "utf8"), config);
    var location = {search: searchText || "", hash: "", pathname: "/people", protocol: "http:",
                    href: "http://127.0.0.1:5000/people" + (searchText || ""),
                    origin: "http://127.0.0.1:5000"};
    function moved(url) {
        var parts = String(url).split("?");
        location.pathname = parts[0];
        location.search = parts.length > 1 ? "?" + parts[1] : "";
    }
    var history = {state: null, scrollRestoration: "auto", pushed: [], replaced: [],
                   pushState: function (state, title, url) {
                       this.pushed.push(url);
                       moved(url);
                   },
                   replaceState: function (state, title, url) {
                       this.replaced.push(url);
                       moved(url);
                   },
                   back: function () {}};
    function answer(url, body) {
        var plain = url.split("?")[0];
        var query = new URLSearchParams(url.split("?")[1] || "");
        if (plain === "/api/people") {
            return response(200, {version: 1, people: LISTED});
        }
        if (plain === "/api/admin/people" && !body) {
            return response(200, {people: {enabled: true, problem: ""},
                                  groups: {faces: 40, grouped: 30, people: 3, named: 2,
                                           other: 6, not_faces: 0, due: false,
                                           grouped_at: grouped.at},
                                  settings: {}});
        }
        if (plain === "/api/admin/people/queue") {
            if (server.building) {
                server.building -= 1;
                return response(202, {building: true, build: 7, items: []});
            }
            return response(200, server.queue || queueAnswer());
        }
        if (plain === "/api/admin/people/suggest") {
            var face = Number(query.get("face"));
            var stale = (server.stale || []).indexOf(face) >= 0;
            var item = (server.suggests || server.queue || queueAnswer()).items
                .filter(function (entry) {
                    return entry.face === face;
                })[0];
            if (item && item.kind === "person") {
                var now = (server.faces || {})[item.person];
                return response(200, {face: face, basis: "person", people: [],
                                      person: {id: item.person, label: item.label,
                                               faces: now === undefined ? item.faces : now,
                                               hidden: (server.hidden || [])
                                                   .indexOf(item.person) >= 0},
                                      preselect: stale ? null : item.to});
            }
            return response(200, {face: face, basis: "face", people: [],
                                  preselect: stale || !item ? null : item.to});
        }
        if (plain === "/api/admin/people/faces") {
            if (body.from === null && (server.moved || []).indexOf(body.faces[0]) >= 0) {
                return response(409, {error: "That face is not where it was any more."});
            }
            tokens += 1;
            return response(200, {person: {id: body.to || 4, label: "Annabel"},
                                  undo: "t" + tokens});
        }
        if (/^\/api\/admin\/people\/\d+\/merge$/.test(plain)) {
            tokens += 1;
            return response(200, {person: {id: body.into, label: "Annabel"},
                                  undo: "t" + tokens});
        }
        if (plain === "/api/admin/people/undo") {
            if (server.undo && server.undo.length) {
                var reply = server.undo.shift();
                return response(reply[0], reply[1]);
            }
            return response(200, {undone: true});
        }
        if (plain === "/api/admin/jobs" && body) {
            // (The grouping finishes at once.)
            grouped.at += 1;
            server.queue = {version: 1, items: [], people: {},
                            counts: {faces: 0, persons: 0, person_faces: 0}};
            return response(202, {job: {id: 1}, added: true});
        }
        return null;
    }
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
        localStorage: server.local || storage(),
        sessionStorage: server.session || storage(),
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
        // (The page's polls: at once.)
        setTimeout: function (callback, ms) {
            return setTimeout(callback, Math.min(ms || 0, 5));
        },
        clearTimeout: clearTimeout,
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function (text) {
            log.error = String(text);
        }},
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch,
        clock: clock
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    vm.runInContext("Date.now = function () { return clock.now; };", context);
    ["site.js", "shell.js", "search-core.js", "people.js"].forEach(function (name) {
        vm.runInContext(read(name), context, {filename: name});
    });
    return {context: context, document: document, log: log, history: history,
            server: server,
            tick: function () {
                clock.now += 1000;
            }};
}

function byId(page, id) {
    return page.document.getElementById(id);
}

function question(page) {
    var node = byId(page, "queue-question");
    return node ? node.textContent : "";
}

function progress(page) {
    var node = byId(page, "queue-progress");
    return node ? node.textContent : "";
}

// A key pressed (on what has the focus), a second after the last.
function press(page, key, extra) {
    page.tick();
    var event = {type: "keydown", key: key, target: page.document.activeElement};
    Object.keys(extra || {}).forEach(function (name) {
        event[name] = extra[name];
    });
    page.document.dispatchEvent(event);
    return event;
}

function button(scope, text) {
    return scope.querySelectorAll("button").filter(function (node) {
        return node.querySelector("span") ? node.querySelector("span").textContent === text
            : node.textContent === text;
    })[0];
}

function sent(page, pattern) {
    return page.log.sent.filter(function (entry) {
        return pattern.test(entry[0]);
    });
}

// Waits for the question to become text (after asking it again); with at, for the
// progress to start so too ("3 of").
function asked(page, text, at) {
    return waitFor(function () {
        return question(page) === text && (!at || progress(page).indexOf(at) === 0);
    }).then(function () {
        return settle(5);
    });
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the list says how many there are to confirm, and Start opens the queue", function () {
    var page = peoplePage("", {building: 2});
    return waitFor(function () {
        return page.document.querySelector(".queue-entry");
    }).then(function (entry) {
        // (Asked again while the server worked it out.)
        assert.ok(/6 to confirm/.test(entry.textContent), entry.textContent);
        var start = entry.querySelectorAll("a").filter(function (node) {
            return node.textContent === "Start";
        })[0];
        assert.strictEqual(start.getAttribute("href") || start.href, "/people?view=queue");
        start.dispatchEvent({type: "click", button: 0});
        return asked(page, "Is this Annabel?");
    }).then(function () {
        assert.deepStrictEqual(page.history.pushed, ["/people?view=queue"]);
        assert.strictEqual(progress(page), "1 of 6");
        // The question has the focus (a screen reader reads it; Enter answers it), with
        // the progress and the closeness read with it.
        var asking = byId(page, "queue-question");
        assert.strictEqual(page.document.activeElement, asking);
        assert.strictEqual(asking.getAttribute("aria-describedby"),
                           "queue-progress queue-words");
        assert.ok(/Very like Annabel/.test(byId(page, "queue-words").textContent));
        // (The progress is one line of the page, there all along: not made anew.)
        var line = byId(page, "queue-progress");
        assert.strictEqual(line.hidden, false);
        // (The faces to compare are pictures only: the heading says whose.)
        page.document.querySelectorAll(".queue-strip").forEach(function (strip) {
            assert.strictEqual(strip.getAttribute("aria-hidden"), "true");
        });
        press(page, "s");
        return asked(page, "Is this Annabel?", "2 of 6").then(function () {
            assert.strictEqual(byId(page, "queue-progress"), line);
            assert.strictEqual(line.textContent, "2 of 6");
            press(page, "Escape");
            return waitFor(function () {
                return byId(page, "people-title").textContent === "People";
            });
        }).then(function () {
            assert.strictEqual(line.hidden, true);
            // Back on the list, the keyboard is on Start.
            assert.strictEqual(page.document.activeElement.textContent, "Start");
        });
    });
});

test("Enter, N, S, X and the arrow answer; stale and moved items are passed over",
     function () {
         var page = peoplePage("?view=queue", {stale: [503], moved: [504]});
         return asked(page, "Is this Annabel?").then(function () {
             // The face, the picture it is in (opens the viewer), Annabel's faces.
             var card = page.document.querySelector(".queue-card");
             assert.ok(/Very like Annabel \(0\.61\)/.test(card.textContent), card.textContent);
             var open = card.querySelector(".queue-picture");
             assert.strictEqual(open.href, "/detail?id=1&face=501");
             assert.strictEqual(card.querySelectorAll(".queue-strip img").length, 3);
             // Each key is a button too, and says its key.
             ["Yes, Annabel", "Not Annabel", "Skip", "Not a face", "Undo", "Leave"]
                 .forEach(function (text) {
                     assert.ok(button(card, text), text);
                 });
             assert.strictEqual(button(card, "Skip").getAttribute("aria-keyshortcuts"),
                                "S ArrowRight");
             assert.ok(button(card, "Undo").disabled);
             press(page, "Enter");
             return asked(page, "Is this Annabel?", "2 of");
         }).then(function () {
             assert.deepStrictEqual(sent(page, /people\/faces$/)[0][1],
                                    {faces: [501], from: null, action: "move", to: 4,
                                     undo: true});
             assert.strictEqual(progress(page), "2 of 6");
             // A face of a video: opened at its moment.
             var open = page.document.querySelector(".queue-picture");
             assert.strictEqual(open.href, "/detail?id=9&at=72.4");
             assert.ok(/Open the video at 1:12/.test(open.textContent));
             press(page, "n");
             // (503 is no longer marked: passed over; 504 was moved by a grouping.)
             return asked(page, "Is this Annabel?", "4 of");
         }).then(function () {
             assert.deepStrictEqual(sent(page, /people\/faces$/)[1][1],
                                    {faces: [502], from: null, action: "reject", person: 4,
                                     undo: true});
             // (503, passed over meanwhile, is said to be.)
             assert.strictEqual(progress(page), "4 of 6 \u2013 1 passed over (changed " +
                                "meanwhile)");
             press(page, "x");
             return asked(page, "Is this Bo?");
         }).then(function () {
             assert.deepStrictEqual(sent(page, /people\/faces$/)[2][1],
                                    {faces: [504], from: null, action: "not_face",
                                     undo: true});
             assert.strictEqual(progress(page), "5 of 6 \u2013 the last one changed since " +
                                "it was shown, so it was passed over");
             press(page, "ArrowRight");
             return asked(page, "Is Person 3 Annabel?");
         }).then(function () {
             var card = page.document.querySelector(".queue-card");
             assert.ok(/Person 3 has 4 faces/.test(card.textContent), card.textContent);
             assert.strictEqual(card.querySelectorAll(".queue-strip")[0]
                 .querySelectorAll("img").length, 3);
             // (No "Not a face" for a person: its place is kept, so that each answer
             // is where it was for a face.)
             assert.ok(!button(card, "Not a face"));
             var keys = card.querySelector(".queue-keys").childNodes;
             assert.deepStrictEqual(keys.map(function (node) {
                 return node.className;
             }).slice(3, 4), ["queue-slot"]);
             assert.strictEqual(keys.length, 6);
             press(page, "x");
             return settle(20);
         }).then(function () {
             assert.strictEqual(question(page), "Is Person 3 Annabel?");
             press(page, "Enter");
             return waitFor(function () {
                 return /all for now/.test(question(page));
             });
         }).then(function () {
             // (With what the person was like when shown: refused if it changed.)
             assert.deepStrictEqual(sent(page, /merge$/),
                                    [["/api/admin/people/3/merge",
                                      {into: 4, face: 31, seen: "s3", undo: true}]]);
             // The tally, and the end has the focus.
             var tally = page.document.querySelector(".queue-tally").textContent;
             assert.strictEqual(tally, "Named 1 face, named 1 person (4 faces), 1 not who " +
                                "was suggested, 1 skipped, 2 passed over (no longer sure, " +
                                "or changed).");
             assert.strictEqual(page.document.activeElement, byId(page, "queue-question"));
             // Skipped 505 is remembered by this browser (of this face data), and can
             // be asked about again.
             assert.ok(/"d1\/f505:5"/.test(page.context.localStorage.getItem(
                 "searchImages.peopleQueueSkips")));
             page.server.stale = [501, 502, 503, 504, 31];
             page.tick();
             button(page.document.querySelector(".queue-card"), "Show the 1 skipped one " +
                    "again").click();
             return asked(page, "Is this Bo?", "5 of 6");
         }).then(function () {
             assert.strictEqual(page.context.localStorage.getItem(
                 "searchImages.peopleQueueSkips"), null);
             press(page, "Enter");
             return waitFor(function () {
                 return /all for now/.test(question(page));
             });
         }).then(function () {
             assert.strictEqual(page.document.querySelector(".queue-tally").textContent,
                                "Named 1 face, 5 passed over (no longer sure, or changed).");
         });
     });

test("with only skipped ones left, the end says so and offers them again", function () {
    var page = peoplePage("?view=queue", {});
    page.context.localStorage.setItem("searchImages.peopleQueueSkips", JSON.stringify(
        ["d1/f501:4", "d1/f502:4", "d1/f503:4", "d1/f504:4", "d1/f505:5", "d1/p3:4"]));
    return waitFor(function () {
        return question(page) === "Nothing to confirm now";
    }).then(function () {
        var card = page.document.querySelector(".queue-card");
        assert.strictEqual(card.querySelector(".queue-tally").textContent,
                           "Only the 6 you skipped are left.");
        // (Read with the end's heading.)
        assert.strictEqual(byId(page, "queue-question").getAttribute("aria-describedby"),
                           "queue-tally");
        assert.strictEqual(byId(page, "queue-progress").hidden, true);
        // (A click in the first moment after the end shows is one meant for the item
        // before it: nothing.)
        button(card, "Show the 6 skipped ones again").click();
        assert.ok(page.context.localStorage.getItem("searchImages.peopleQueueSkips"));
        page.tick();
        button(card, "Show the 6 skipped ones again").click();
        return asked(page, "Is this Annabel?", "1 of 6");
    });
});

test("Undo takes the answers back one after another", function () {
    var page = peoplePage("?view=queue");
    return asked(page, "Is this Annabel?").then(function () {
        press(page, "Enter");
        return waitFor(function () {
            return progress(page) === "2 of 6";
        });
    }).then(function () {
        press(page, "s");
        return waitFor(function () {
            return progress(page) === "3 of 6";
        });
    }).then(function () {
        press(page, "n");
        return waitFor(function () {
            return progress(page) === "4 of 6";
        });
    }).then(function () {
        assert.ok(!button(page.document.querySelector(".queue-card"), "Undo").disabled);
        // Ctrl+Z: the "Not" of 503 (the server's undo, by its token); the line says so.
        press(page, "z", {ctrlKey: true});
        return waitFor(function () {
            return progress(page) === "3 of 6 \u2013 undone: Not Annabel";
        });
    }).then(function () {
        assert.deepStrictEqual(sent(page, /undo$/).map(function (entry) {
            return entry[1];
        }), [{undo: "t2"}]);
        // U: the skip (this browser's), then the first answer (the server's again).
        press(page, "u");
        return waitFor(function () {
            return progress(page) === "2 of 6 \u2013 undone: Skip";
        });
    }).then(function () {
        assert.strictEqual(page.context.localStorage.getItem("searchImages.peopleQueueSkips"),
                           null);
        button(page.document.querySelector(".queue-card"), "Undo").click();
        return settle(20);
    }).then(function () {
        // (A click right after the item showed decides nothing.)
        assert.strictEqual(progress(page), "2 of 6 \u2013 undone: Skip");
        page.tick();
        button(page.document.querySelector(".queue-card"), "Undo").click();
        return waitFor(function () {
            return progress(page) === "1 of 6 \u2013 undone: Yes, Annabel";
        });
    }).then(function () {
        assert.deepStrictEqual(sent(page, /undo$/).map(function (entry) {
            return entry[1];
        }), [{undo: "t2"}, {undo: "t1"}]);
        assert.ok(button(page.document.querySelector(".queue-card"), "Undo").disabled);
        // (Nothing more: the line says so, no message over the answers.)
        press(page, "u");
        assert.strictEqual(progress(page), "1 of 6 \u2013 nothing to undo");
    });
});

test("a press right after an item shows, or a key held down, decides nothing",
     function () {
         var page = peoplePage("?view=queue");
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");
             return waitFor(function () {
                 return progress(page) === "2 of 6";
             });
         }).then(function () {
             // (Enter again at once, no second later: meant for the one before.)
             page.document.dispatchEvent({type: "keydown", key: "Enter",
                                          target: page.document.activeElement});
             page.tick();
             press(page, "Enter", {repeat: true});
             return settle(30);
         }).then(function () {
             assert.strictEqual(sent(page, /people\/faces$/).length, 1);
             assert.strictEqual(progress(page), "2 of 6");
             // Enter on a button does what the button says (here: Skip), not yes.
             var skip = button(page.document.querySelector(".queue-card"), "Skip");
             skip.focus();
             press(page, "Enter");
             return settle(30);
         }).then(function () {
             assert.strictEqual(sent(page, /people\/faces$/).length, 1);
             assert.strictEqual(progress(page), "2 of 6");
         });
     });

test("Esc leaves; the end groups the faces again to look for more", function () {
    var page = peoplePage("?view=queue", {queue: {version: 1, items: [
        {kind: "face", face: 501, to: 4, score: 0.6, lead: 0.3, words: "very like",
         image: 1}], people: {"4": {id: 4, label: "Annabel", name: "Annabel", faces: 6,
                                   show: [41]}},
        counts: {faces: 1, persons: 0, person_faces: 0}}});
    return asked(page, "Is this Annabel?").then(function () {
        press(page, "Enter");
        return waitFor(function () {
            return /all for now/.test(question(page));
        });
    }).then(function () {
        // (Not in the first moment: a click meant for the last item.)
        button(page.document.querySelector(".queue-card"), "Group faces again to look " +
               "for more").click();
        assert.deepStrictEqual(sent(page, /jobs$/), []);
        page.tick();
        button(page.document.querySelector(".queue-card"), "Group faces again to look " +
               "for more").click();
        // (The message takes the keyboard: the button went.)
        assert.ok(/Grouping the faces again/.test(page.document.activeElement.textContent));
        return waitFor(function () {
            return question(page) === "Nothing to confirm now";
        });
    }).then(function () {
        assert.deepStrictEqual(sent(page, /jobs$/), [["/api/admin/jobs",
                                                      {kind: "group_faces",
                                                       library_id: null}]]);
        press(page, "Escape");
        return waitFor(function () {
            return byId(page, "people-title").textContent === "People";
        });
    }).then(function () {
        assert.strictEqual(page.history.pushed[page.history.pushed.length - 1], "/people");
        return settle(30);
    }).then(function () {
        // Nothing more to confirm: no entry.
        assert.ok(!page.document.querySelector(".queue-entry"));
    });
});

test("skipped ones are not counted until shown again", function () {
    var page = peoplePage("");
    // (Skips of another face data -- Delete face data numbers faces anew -- hide
    // nothing.)
    page.context.localStorage.setItem("searchImages.peopleQueueSkips",
                                      JSON.stringify(["d1/f501:4", "d1/p3:4", "d0/f502:4"]));
    return waitFor(function () {
        return page.document.querySelector(".queue-entry");
    }).then(function (entry) {
        assert.ok(/4 to confirm/.test(entry.textContent), entry.textContent);
        var again = button(entry, "Show the 2 skipped ones again");
        assert.ok(again);
        again.focus();
        again.click();
        return waitFor(function () {
            return /6 to confirm/.test(page.document.querySelector(".queue-entry")
                .textContent);
        });
    }).then(function () {
        // (The button went: the keyboard is on Start.)
        assert.strictEqual(page.document.activeElement.textContent, "Start");
        assert.strictEqual(page.context.localStorage.getItem("searchImages.peopleQueueSkips"),
                           null);
    });
});

test("the queue is asked for again with the number of the build on its way", function () {
    var page = peoplePage("", {building: 2});
    return waitFor(function () {
        return page.document.querySelector(".queue-entry");
    }).then(function () {
        assert.deepStrictEqual(page.log.requests.filter(function (url) {
            return /people\/queue/.test(url);
        }), ["/api/admin/people/queue", "/api/admin/people/queue?build=7",
             "/api/admin/people/queue?build=7"]);
    });
});

test("a person hidden, or with other faces, meanwhile is passed over", function () {
    var answer = queueAnswer();
    answer.items = answer.items.slice(5).concat([
        {kind: "person", person: 8, label: "Person 8", face: 81, faces: 3, show: [81, 82],
         seen: "s8", to: 4, score: 0.6, lead: 0.3, words: "very like"},
        {kind: "person", person: 9, label: "Person 9", face: 91, faces: 2, show: [91],
         seen: "s9", to: 4, score: 0.6, lead: 0.3, words: "very like"}]);
    var page = peoplePage("?view=queue", {queue: answer, hidden: [3], faces: {8: 5}});
    return asked(page, "Is Person 9 Annabel?").then(function () {
        assert.strictEqual(progress(page), "3 of 3 \u2013 2 passed over (changed meanwhile)");
        press(page, "n");
        return waitFor(function () {
            return /all for now/.test(question(page));
        });
    }).then(function () {
        // "Not Annabel" for all of its faces, with what it was like when shown.
        assert.deepStrictEqual(sent(page, /people\/faces$/)[0][1],
                               {faces: "all", from: 9, seen: "s9", action: "reject",
                                person: 4, undo: true});
    });
});

test("Undo: a busy server or a later change keeps the answers, a refusal lets them go",
     function () {
         var page = peoplePage("?view=queue", {undo: [
             [503, {error: "The face data is busy: try again."}],
             [409, {error: "A later change has to be undone first.", kept: true}],
             [409, {error: "That change cannot be undone here any more."}]]});
         var card = function () {
             return page.document.querySelector(".queue-card");
         };
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");
             return waitFor(function () {
                 return progress(page) === "2 of 6";
             });
         }).then(function () {
             // Busy, then a later change first: the answer can still be undone.
             press(page, "u");
             return settle(20);
         }).then(function () {
             press(page, "u");
             return settle(20);
         }).then(function () {
             assert.strictEqual(progress(page), "2 of 6");
             press(page, "u");
             return settle(20);
         }).then(function () {
             assert.deepStrictEqual(sent(page, /undo$/).map(function (entry) {
                 return entry[1].undo;
             }), ["t1", "t1", "t1"]);
             // Refused for good: no more asking.
             assert.ok(!button(card(), "Undo").disabled);
             press(page, "u");
             return settle(20);
         }).then(function () {
             assert.strictEqual(sent(page, /undo$/).length, 3);
             assert.ok(/cannot be undone here/.test(progress(page)), progress(page));
         });
     });

test("leaving after answers draws the list from the people as they are now", function () {
    var page = peoplePage("");
    var asks = function (pattern) {
        return page.log.requests.filter(function (url) {
            return pattern.test(url);
        }).length;
    };
    return waitFor(function () {
        return page.document.querySelector(".queue-entry");
    }).then(function (entry) {
        assert.strictEqual(asks(/^\/api\/people(\?|$)/), 1);
        entry.querySelectorAll("a")[0].dispatchEvent({type: "click", button: 0});
        return asked(page, "Is this Annabel?");
    }).then(function () {
        press(page, "Enter");
        return waitFor(function () {
            return progress(page) === "2 of 6";
        });
    }).then(function () {
        // (The server still works the new queue out: the entry shows the one kept.)
        page.server.building = 1000;
        press(page, "Escape");
        return waitFor(function () {
            return page.document.querySelector(".queue-entry");
        });
    }).then(function (entry) {
        // The people and the status again; the count without the face named, at once.
        assert.strictEqual(asks(/^\/api\/people(\?|$)/), 2);
        assert.strictEqual(asks(/^\/api\/admin\/people$/), 2);
        assert.ok(/5 to confirm/.test(entry.textContent), entry.textContent);
        assert.strictEqual(page.document.activeElement.textContent, "Start");
    });
});

/* ---------- Leaving, coming back, a reload: Undo goes on; notes told; the list ------ */

var UNDO_KEY = "searchImages.peopleQueueUndo";

// The queue without the items of the given faces (the server's queue after they were
// answered), of the face data data (default "d1").
function queueWithout(answered, data) {
    var answer = queueAnswer();
    answer.items = answer.items.filter(function (item) {
        return answered.indexOf(item.face) < 0;
    });
    if (data) {
        answer.data = data;
    }
    return answer;
}

function undoSent(page) {
    return sent(page, /undo$/).map(function (entry) {
        return entry[1].undo;
    });
}

function card(page) {
    return page.document.querySelector(".queue-card");
}

// The messages shown at the foot of the screen ("" while there are none).
function toasts(page) {
    var region = byId(page, "shell-toasts");
    return region ? region.textContent : "";
}

// The tokens this tab keeps for Undo (null: none).
function keptTokens(page) {
    var kept = page.context.sessionStorage.getItem(UNDO_KEY);
    return kept === null ? null : JSON.parse(kept).steps.map(function (step) {
        return step.token;
    });
}

function oneFace() {
    return {version: 1, data: "d1", items: [
        {kind: "face", face: 501, to: 4, score: 0.6, lead: 0.3, words: "very like",
         image: 1}], people: {"4": {id: 4, label: "Annabel", name: "Annabel", faces: 6,
                                   show: [41]}},
        counts: {faces: 1, persons: 0, person_faces: 0}};
}

test("Esc, Leave and All people right after an item shows do not leave", function () {
    var page = peoplePage("?view=queue", {queue: oneFace()});
    return asked(page, "Is this Annabel?").then(function () {
        // (No second later: a press meant for the item before.)
        page.document.dispatchEvent({type: "keydown", key: "Escape",
                                     target: page.document.activeElement});
        button(card(page), "Leave").click();
        return settle(20);
    }).then(function () {
        assert.strictEqual(question(page), "Is this Annabel?");
        assert.deepStrictEqual(page.history.pushed, []);
        press(page, "Enter");
        return waitFor(function () {
            return /all for now/.test(question(page));
        });
    }).then(function () {
        var all = card(page).querySelectorAll("a").filter(function (node) {
            return node.textContent === "All people";
        })[0];
        assert.strictEqual(all.getAttribute("href") || all.href, "/people");
        all.dispatchEvent({type: "click", button: 0});
        page.document.dispatchEvent({type: "keydown", key: "Escape",
                                     target: page.document.activeElement});
        return settle(20).then(function () {
            assert.ok(/all for now/.test(question(page)));
            assert.deepStrictEqual(page.history.pushed, []);
            page.tick();
            all.dispatchEvent({type: "click", button: 0});
            return waitFor(function () {
                return byId(page, "people-title").textContent === "People";
            });
        });
    }).then(function () {
        assert.deepStrictEqual(page.history.pushed, ["/people"]);
    });
});

test("Esc held down from the moment an item shows does not leave", function () {
    var page = peoplePage("?view=queue", {queue: oneFace()});
    return asked(page, "Is this Annabel?").then(function () {
        // (Pressed in the first moment, and still held when it has passed.)
        page.document.dispatchEvent({type: "keydown", key: "Escape",
                                     target: page.document.activeElement});
        press(page, "Escape", {repeat: true});
        return settle(30);
    }).then(function () {
        assert.strictEqual(question(page), "Is this Annabel?");
        assert.deepStrictEqual(page.history.pushed, []);
        // A press of its own leaves.
        press(page, "Escape");
        return waitFor(function () {
            return byId(page, "people-title").textContent === "People";
        });
    }).then(function () {
        assert.deepStrictEqual(page.history.pushed, ["/people"]);
    });
});

test("after leaving and coming back, U takes the answers back, the last one first",
     function () {
         var page = peoplePage("?view=queue");
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");       // (501)
             return waitFor(function () {
                 return progress(page) === "2 of 6";
             });
         }).then(function () {
             press(page, "s");           // (502: skipped, this browser keeps it)
             return waitFor(function () {
                 return progress(page) === "3 of 6";
             });
         }).then(function () {
             press(page, "n");           // (503)
             return waitFor(function () {
                 return progress(page) === "4 of 6";
             });
         }).then(function () {
             press(page, "Enter");       // (504)
             return waitFor(function () {
                 return progress(page) === "5 of 6";
             });
         }).then(function () {
             assert.deepStrictEqual(keptTokens(page), ["t1", "t2", "t3"]);
             // (The server's queue without the faces answered; undone, they are marked
             // again.)
             page.server.queue = queueWithout([501, 503, 504]);
             page.server.suggests = queueAnswer();
             press(page, "Escape");
             return waitFor(function () {
                 return page.document.querySelector(".queue-entry");
             });
         }).then(function (entry) {
             // (The list offers it too.)
             assert.ok(button(entry, "Undo last answer"), entry.textContent);
             entry.querySelectorAll("a")[0].dispatchEvent({type: "click", button: 0});
             return asked(page, "Is this Bo?", "1 of 2");
         }).then(function () {
             assert.ok(!button(card(page), "Undo").disabled);
             press(page, "u");
             return waitFor(function () {
                 return progress(page) === "1 of 3 \u2013 undone: Yes, Annabel";
             });
         }).then(function () {
             // The face of the last answer, asked about again.
             assert.strictEqual(question(page), "Is this Annabel?");
             assert.strictEqual(page.document.querySelector(".queue-picture").href,
                                "/detail?id=3&face=504");
             press(page, "z", {ctrlKey: true});
             return waitFor(function () {
                 return progress(page) === "1 of 4 \u2013 undone: Not Annabel";
             });
         }).then(function () {
             assert.strictEqual(page.document.querySelector(".queue-picture").href,
                                "/detail?id=2&face=503");
             press(page, "u");
             return waitFor(function () {
                 return progress(page) === "1 of 5 \u2013 undone: Yes, Annabel";
             });
         }).then(function () {
             assert.deepStrictEqual(undoSent(page), ["t3", "t2", "t1"]);
             assert.strictEqual(keptTokens(page), null);
             // (The skip is not among them: "Show the skipped one again" brings it.)
             press(page, "u");
             assert.strictEqual(progress(page), "1 of 5 \u2013 nothing to undo");
             // Answered again, it can be undone again.
             press(page, "n");
             return waitFor(function () {
                 return progress(page) === "2 of 5";
             });
         }).then(function () {
             assert.deepStrictEqual(sent(page, /people\/faces$/)[3][1],
                                    {faces: [501], from: null, action: "reject", person: 4,
                                     undo: true});
             assert.deepStrictEqual(keptTokens(page), ["t4"]);
             press(page, "u");
             return waitFor(function () {
                 return progress(page) === "1 of 5 \u2013 undone: Not Annabel";
             });
         }).then(function () {
             assert.deepStrictEqual(undoSent(page), ["t3", "t2", "t1", "t4"]);
         });
     });

test("a reload keeps the answers to undo, of this face data only", function () {
    var first = peoplePage("?view=queue");
    return asked(first, "Is this Annabel?").then(function () {
        press(first, "Enter");
        return waitFor(function () {
            return progress(first) === "2 of 6";
        });
    }).then(function () {
        press(first, "n");
        return waitFor(function () {
            return progress(first) === "3 of 6";
        });
    }).then(function () {
        // The page opened again in this tab, the queue without the two answered.
        var again = peoplePage("?view=queue", {session: first.context.sessionStorage,
                                               local: first.context.localStorage,
                                               queue: queueWithout([501, 502]),
                                               suggests: queueAnswer()});
        return asked(again, "Is this Annabel?", "1 of 4").then(function () {
            assert.ok(!button(card(again), "Undo").disabled);
            press(again, "u");
            return waitFor(function () {
                return progress(again) === "1 of 5 \u2013 undone: Not Annabel";
            });
        }).then(function () {
            assert.deepStrictEqual(undoSent(again), ["t2"]);
            assert.strictEqual(again.document.querySelector(".queue-picture").href,
                               "/detail?id=9&at=72.4");
            assert.deepStrictEqual(keptTokens(again), ["t1"]);
            // Of other face data (Delete face data numbers the faces anew): let go.
            var other = peoplePage("?view=queue", {session: first.context.sessionStorage,
                                                   queue: queueWithout([501, 502], "d2")});
            return asked(other, "Is this Annabel?", "1 of 4").then(function () {
                assert.ok(button(card(other), "Undo").disabled);
                press(other, "u");
                assert.strictEqual(progress(other), "1 of 4 \u2013 nothing to undo");
                assert.strictEqual(keptTokens(other), null);
                assert.deepStrictEqual(undoSent(other), []);
            });
        });
    });
});

test("this tab lets the answers go when the server did, not when it was busy",
     function () {
         var page = peoplePage("?view=queue", {undo: [
             [503, {error: "The face data is busy: try again."}],
             [409, {error: "A later change has to be undone first.", kept: true}],
             [409, {error: "That change cannot be undone here any more."}]]});
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");
             return waitFor(function () {
                 return progress(page) === "2 of 6";
             });
         }).then(function () {
             press(page, "u");
             return settle(20);
         }).then(function () {
             press(page, "u");
             return settle(20);
         }).then(function () {
             assert.deepStrictEqual(keptTokens(page), ["t1"]);
             press(page, "u");
             return settle(20);
         }).then(function () {
             assert.strictEqual(keptTokens(page), null);
         });
     });

test("the list offers Undo last answer while an answer can be undone", function () {
    var page = peoplePage("?view=queue", {queue: oneFace()});
    var entry = function () {
        return page.document.querySelector(".queue-entry");
    };
    return asked(page, "Is this Annabel?").then(function () {
        press(page, "Enter");
        return waitFor(function () {
            return /all for now/.test(question(page));
        });
    }).then(function () {
        page.server.queue = queueWithout([501, 502, 503, 504, 505, 31]);
        press(page, "Escape");
        return waitFor(entry);
    }).then(function () {
        // Nothing more to confirm, but the answer can still be undone.
        assert.ok(/Nothing more to confirm now/.test(entry().textContent), entry().textContent);
        var undo = button(entry(), "Undo last answer");
        assert.ok(/Yes, Annabel/.test(undo.title), undo.title);
        assert.strictEqual(page.document.activeElement, undo);
        // (Undone, the face is to be confirmed again.)
        page.server.queue = oneFace();
        undo.click();
        return waitFor(function () {
            return entry() && /1 to confirm/.test(entry().textContent);
        });
    }).then(function () {
        assert.deepStrictEqual(undoSent(page), ["t1"]);
        assert.strictEqual(keptTokens(page), null);
        assert.ok(!button(entry(), "Undo last answer"));
        assert.ok(/Undone: Yes, Annabel\./.test(toasts(page)), toasts(page));
        assert.strictEqual(page.document.activeElement.textContent, "Start");
    });
});

test("the list's Undo last answer, refused for good, goes; refused for now, stays",
     function () {
         var page = peoplePage("?view=queue", {queue: oneFace(), undo: [
             [409, {error: "A later change has to be undone first.", kept: true}],
             [409, {error: "That change cannot be undone here any more."}]]});
         var entry = function () {
             return page.document.querySelector(".queue-entry");
         };
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");
             return waitFor(function () {
                 return /all for now/.test(question(page));
             });
         }).then(function () {
             page.server.queue = queueWithout([501, 502, 503, 504, 505, 31]);
             press(page, "Escape");
             return waitFor(entry);
         }).then(function () {
             button(entry(), "Undo last answer").click();
             return waitFor(function () {
                 return /later change/.test(toasts(page));
             });
         }).then(function () {
             // (Kept: it can be undone once the later change is.)
             assert.deepStrictEqual(keptTokens(page), ["t1"]);
             var undo = button(entry(), "Undo last answer");
             assert.ok(undo && !undo.disabled);
             undo.click();
             return waitFor(function () {
                 return /any more/.test(toasts(page));
             });
         }).then(function () {
             assert.strictEqual(keptTokens(page), null);
             assert.ok(!entry() || !button(entry(), "Undo last answer"));
             assert.deepStrictEqual(undoSent(page), ["t1", "t1"]);
         });
     });

test("Group faces again lets the answers' undo go (a grouping takes it away)",
     function () {
         var page = peoplePage("?view=queue", {queue: oneFace()});
         return asked(page, "Is this Annabel?").then(function () {
             press(page, "Enter");
             return waitFor(function () {
                 return /all for now/.test(question(page));
             });
         }).then(function () {
             press(page, "Escape");
             return waitFor(function () {
                 return page.document.querySelector(".queue-entry");
             });
         }).then(function () {
             assert.deepStrictEqual(keptTokens(page), ["t1"]);
             button(byId(page, "people-actions"), "Group faces again").click();
             return waitFor(function () {
                 return keptTokens(page) === null;
             });
         }).then(function () {
             return settle(20);
         }).then(function () {
             assert.ok(!page.document.querySelector(".queue-entry"));
         });
     });

test("Undo's notes are told to screen readers, the progress line is not told twice",
     function () {
         var page = peoplePage("?view=queue", {undo: [
             [409, {error: "That change cannot be undone here any more."}]]});
         var told = function () {
             return byId(page, "queue-say");
         };
         return asked(page, "Is this Annabel?").then(function () {
             // A polite live region of the page, read only by screen readers; the
             // progress line is read with each question (aria-describedby).
             assert.strictEqual(told().getAttribute("aria-live"), "polite");
             assert.strictEqual(told().className, "visually-hidden");
             assert.strictEqual(byId(page, "queue-progress").getAttribute("aria-live"), null);
             assert.strictEqual(told().textContent, "");
             press(page, "u");
             assert.strictEqual(progress(page), "1 of 6 \u2013 nothing to undo");
             assert.strictEqual(told().textContent, "nothing to undo");
             // (The same note again is a change too: told again.)
             press(page, "u");
             assert.strictEqual(told().textContent, "nothing to undo\u00a0");
             press(page, "u");
             assert.strictEqual(told().textContent, "nothing to undo");
             press(page, "Enter");
             return waitFor(function () {
                 return progress(page) === "2 of 6";
             });
         }).then(function () {
             // (Another item: nothing left to tell.)
             assert.strictEqual(told().textContent, "");
             press(page, "u");           // (refused for good by the server)
             return settle(20);
         }).then(function () {
             press(page, "u");
             assert.ok(/cannot be undone here/.test(told().textContent), told().textContent);
             assert.strictEqual(progress(page), "2 of 6 \u2013 " + told().textContent);
             press(page, "Escape");
             return waitFor(function () {
                 return byId(page, "people-title").textContent === "People";
             });
         }).then(function () {
             assert.strictEqual(told().textContent, "");
         });
     });

test("the list is drawn with Who's this? when it comes at once, without it when not",
     function () {
         function opened(server) {
             var page = peoplePage("", server);
             page.drawn = [];
             // (When the people show: is the entry there already, or does it come
             // later and push them down?)
             page.document.onAppend = function (child) {
                 if (child.className === "people-grid") {
                     page.drawn.push(Boolean(page.document.querySelector(".queue-entry")));
                 }
             };
             return page;
         }
         var page = opened({});
         return waitFor(function () {
             return page.drawn.length;
         }).then(function () {
             assert.deepStrictEqual(page.drawn, [true]);
             return settle(20);
         }).then(function () {
             // (Asked for once.)
             assert.deepStrictEqual(page.log.requests.filter(function (url) {
                 return /people\/queue/.test(url);
             }), ["/api/admin/people/queue"]);
             // A queue still being worked out: the list does not wait for it.
             var slow = opened({building: 1000});
             return waitFor(function () {
                 return slow.drawn.length;
             }).then(function () {
                 assert.deepStrictEqual(slow.drawn, [false]);
                 slow.server.building = 0;
             });
         });
     });

test("on a phone the answers stay at the foot of the screen, each in its place", function () {
    var css = read("people.css");
    var phone = /@media \(max-width: 600px\), \(max-height: 520px\) \{([\s\S]*?)\n\}/
        .exec(css);
    assert.ok(phone, "the bar's block");
    // A bar fixed at the foot of the screen (not after the card: its height moves it),
    // three a row of one height, a person's "Not a face" place kept.
    var keys = /\.queue-card:not\(\.queue-end\) \.queue-keys \{([^}]*)\}/.exec(phone[1])[1];
    assert.ok(/position: fixed;/.test(keys) && /bottom: 0;/.test(keys), keys);
    assert.ok(/grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);/.test(keys), keys);
    assert.ok(/grid-auto-rows: 48px;/.test(keys), keys);
    assert.ok(/\.queue-slot \{\s*display: block;/.test(phone[1]));
    assert.ok(/-webkit-line-clamp: 2;/.test(phone[1]));
    // (Messages above the bar, not over it.)
    assert.ok(/\.people-queue-view #shell-toasts \{\s*bottom: calc\(132px/.test(phone[1]));
    var small = /@media \(max-width: 600px\) \{([\s\S]*?)\n\}/g;
    var block;
    var lead = false;
    while ((block = small.exec(css)) !== null) {
        lead = lead || /\.people-queue-view #people-lead \{\s*display: none;/.test(block[1]);
    }
    assert.ok(lead);
    // (The key hints as clear as the buttons' words.)
    assert.ok(!/\.queue-key \{[^}]*opacity/.test(css));
    // The keys' names show only where there is a keyboard.
    assert.ok(/\.queue-key \{[^}]*display: none;/.test(css));
    assert.ok(/@media \(hover: hover\) and \(pointer: fine\) \{\s*\.queue-key \{\s*display: inline-block;/
        .test(css));
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
