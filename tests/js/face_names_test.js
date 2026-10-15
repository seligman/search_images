/* Tests for naming a face where it is, in the viewer and on the details page
   (viewer.js openNaming, format.faceChoices), under Node (no packages needed):
   the real page scripts on the made-up DOM of tests/js/app_meaning_test.js, with a
   fake fetch() for the server's answers. The choices for what was typed; a face box
   is a button (a tap, a click or Enter opens its popover; a drag does not); a
   person taken with a few letters and Enter, a new name for an unnamed person (all
   its faces) or for this face alone, "Not <name>", "Not a face"; the pane and the
   boxes follow; Undo (its message, Ctrl+Z); Esc gives the keyboard back; a video's
   faces in the pane; nothing of it for visitors; the search pages' people asked for
   again. All people and pictures are made up.

   Run by tests/test_face_names.py, or directly:
       node tests/js/face_names_test.js
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

/* The details page's scripts as the admin (admin false: a visitor) sees them, with
   answer(path, body) for each request (a promise of a response, or null: 404).
   page.log.sent: [[path, body]] of the requests with a body. */
function serverPage(answer, admin) {
    var log = {requests: [], sent: []};
    var document = FAKE.makeDocument();
    var create = document.createElement;
    document.createElement = function (tag) {
        var node = create.apply(document, arguments);
        if (!node.select) {
            node.select = function () {};
        }
        if (String(tag).toLowerCase() === "input" && node.value === undefined) {
            node.value = "";
        }
        return node;
    };
    var config = {mode: "server", page: "detail", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: admin !== false, static: false, people: true}};
    FAKE.parseBody(document, fs.readFileSync(path.join(ROOT, "templates", "detail.html"),
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
    var listeners = {};
    function fetch(url, init) {
        log.requests.push(String(url));
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (body) {
            log.sent.push([String(url), body]);
        }
        return answer(String(url), body) || response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: "", hash: "", pathname: "/detail", protocol: "http:",
                   href: "http://127.0.0.1:5000/detail", origin: "http://127.0.0.1:5000"},
        history: {state: null, pushState: function () {}, replaceState: function () {},
                  back: function () {}},
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
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function (name, handler) {
        (listeners[name] = listeners[name] || []).push(handler);
    };
    context.removeEventListener = function (name, handler) {
        listeners[name] = (listeners[name] || []).filter(function (other) {
            return other !== handler;
        });
    };
    vm.createContext(context);
    ["site.js", "shell.js", "search-core.js", "viewer.js"].forEach(function (name) {
        vm.runInContext(read(name), context, {filename: name});
    });
    return {context: context, document: document, log: log};
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- What the popover offers ---------------------------------------------- */

var PEOPLE = [
    {id: 5, name: "Ada Lind", label: "Ada Lind", cover: 11, faces: 9, ids: [1, 2, 3]},
    {id: 6, name: "Bram", label: "Bram", cover: 12, faces: 4, ids: [2]},
    {id: 7, name: "Dot", label: "Dot", cover: 13, faces: 3, ids: [4], hidden: true},
    {id: 8, name: "Adele", label: "Adele", cover: 14, faces: 2, ids: [5]},
    {id: 12, name: "", label: "Person 12", cover: 15, faces: 37, ids: [1, 6]},
    {id: 120, name: "", label: "Person 120", cover: 16, faces: 5, ids: [7]}
];

function choices(face, text) {
    var format = require(path.join(STATIC, "viewer.js")).format;
    return format.faceChoices(PEOPLE, face, text).map(function (choice) {
        return choice.kind === "person" ? choice.person.label : choice.kind + ":" +
            (choice.kind === "name" ? choice.person + ":" : "") + choice.name;
    });
}

test("the choices for what was typed", function () {
    var nobody = {person: null, named: false};
    // Nothing typed: the named people (hidden ones too), as /api/people lists them.
    assert.deepStrictEqual(choices(nobody, ""), ["Ada Lind", "Bram", "Dot", "Adele"]);
    assert.deepStrictEqual(choices(nobody, "  "), ["Ada Lind", "Bram", "Dot", "Adele"]);
    // A few letters: those whose name (or a word of it) starts so; then a new name.
    assert.deepStrictEqual(choices(nobody, "ad"), ["Ada Lind", "Adele", "new:ad"]);
    assert.deepStrictEqual(choices(nobody, "LIN"), ["Ada Lind", "new:LIN"]);
    assert.deepStrictEqual(choices(nobody, "d"), ["Dot", "new:d"]);
    // (Not a part in the middle of a word: "ram" is a new name, not Bram.)
    assert.deepStrictEqual(choices(nobody, "ram"), ["new:ram"]);
    // The one called so first, and then no new person of that name.
    assert.deepStrictEqual(choices(nobody, "adele"), ["Adele"]);
    assert.deepStrictEqual(choices(nobody, "ada lind"), ["Ada Lind"]);
    assert.deepStrictEqual(choices(nobody, "  ada   LIND "), ["Ada Lind"]);
    // Accents and case aside -- but the server tells names with accents apart: a new
    // name too.
    assert.deepStrictEqual(choices(nobody, "\u00c1d\u00e8le"), ["Adele", "new:\u00c1d\u00e8le"]);
    // People without a name only by their number.
    assert.deepStrictEqual(choices(nobody, "12"), ["Person 12", "Person 120", "new:12"]);
    assert.deepStrictEqual(choices(nobody, "person 12"), ["Person 12", "Person 120"]);
    assert.deepStrictEqual(choices(nobody, "pe"), ["new:pe"]);
    // ("Person 5" is how a person without a name is shown: never a new name.)
    assert.deepStrictEqual(choices(nobody, "Person 5"), []);
});

test("a face of an unnamed person can name it, or go alone", function () {
    var unnamed = {person: 12, named: false};
    // (Not its own person among the choices.)
    assert.deepStrictEqual(choices(unnamed, "12"), ["Person 120", "name:12:12", "new:12"]);
    assert.deepStrictEqual(choices(unnamed, "Nell"), ["name:12:Nell", "new:Nell"]);
    assert.deepStrictEqual(choices(unnamed, "ad"), ["Ada Lind", "Adele", "name:12:ad",
                                                    "new:ad"]);
    // A named person's face: another person, or a new one of this face alone.
    var named = {person: 5, named: true};
    assert.deepStrictEqual(choices(named, ""), ["Bram", "Dot", "Adele"]);
    assert.deepStrictEqual(choices(named, "ad"), ["Adele", "new:ad"]);
    // Its own name typed: nothing to do.
    assert.deepStrictEqual(choices(named, "ada lind"), []);
    assert.deepStrictEqual(choices({person: null}, "Nell"), ["new:Nell"]);
    var format = require(path.join(STATIC, "viewer.js")).format;
    assert.deepStrictEqual(format.faceChoices(null, null, "x").map(function (item) {
        return item.kind;
    }), ["new"]);
});

/* ---------- The popover on a picture --------------------------------------------- */

function faces() {
    return [
        {id: 301, x: 0.1, y: 0.2, w: 0.1, h: 0.15, person: 5, label: "Ada Lind", quality: 0.9},
        {id: 302, x: 0.4, y: 0.2, w: 0.1, h: 0.15, person: 12, label: "Person 12",
         quality: 0.8},
        {id: 303, x: 0.7, y: 0.2, w: 0.08, h: 0.12, person: null, label: "", quality: 0.5}
    ];
}

/* A picture on the details page with its faces shown, the server answering:
   /api/people (PEOPLE), /api/image/4 (the picture's people now: state.people),
   and the admin's changes (reply(path, body), or a reply of its own). */
function pictureWithFaces(options) {
    options = options || {};
    var state = {people: {names: [{id: 5, label: "Ada Lind", named: true},
                                  {id: 12, label: "Person 12", named: false}],
                          faces: faces()}};
    var page = serverPage(function (url, body) {
        if (url === "/api/people?hidden=1") {
            return response(200, {version: 1, people: PEOPLE});
        }
        if (url === "/api/image/4") {
            return response(200, {id: 4, people: state.people});
        }
        if (url.indexOf("/api/admin/") === 0) {
            var path = url.slice("/api/admin".length);
            if (options.reply) {
                return options.reply(path, body, state);
            }
            if (path === "/people/undo") {
                state.people = {names: [{id: 5, label: "Ada Lind", named: true},
                                        {id: 12, label: "Person 12", named: false}],
                                faces: faces()};
                return response(200, {undone: true});
            }
            return response(200, {person: {id: 5, label: "Ada Lind", faces: 10},
                                  undo: "token-1"});
        }
        return null;
    }, options.admin);
    page.context.localStorage.setItem("searchImages.faceBoxes", "1");
    var doc = page.document;
    var rec = options.rec || {i: 4, l: "lib", t: "Garden", w: 1600, hgt: 1200};
    var stage = {rec: rec, mode: options.mode || "image", img: doc.createElement("img"),
                 box: doc.createElement("div"), seekTo: function (seconds) {
                     page.seeks.push(seconds);
                     return true;
                 }};
    doc.body.appendChild(stage.box);
    stage.box.appendChild(stage.img);
    var info = doc.createElement("div");
    doc.body.appendChild(info);
    var detail = {id: 4, people: options.people || state.people};
    page.seeks = [];
    page.state = state;
    page.stage = stage;
    page.info = info;
    page.detail = detail;
    page.rec = rec;
    page.render = function () {
        page.context.VIEWER.renderInfo(info, rec, detail, {get: function () {
            return null;
        }, stage: stage});
    };
    page.render();
    return page;
}

function boxes(page) {
    return page.stage.box.querySelectorAll(".face-box").map(function (box) {
        return box.getAttribute("data-face") + ":" + box.textContent;
    });
}

function box(page, id) {
    return page.stage.box.querySelector(".face-box[data-face=\"" + id + "\"]");
}

function popover(page) {
    return page.document.querySelector(".face-popover");
}

function key(node, name, extra) {
    var event = {type: "keydown", key: name};
    Object.keys(extra || {}).forEach(function (field) {
        event[field] = extra[field];
    });
    node.dispatchEvent(event);
    return event;
}

function type(page, text) {
    var input = popover(page).querySelector(".face-pop-input");
    input.value = text;
    input.dispatchEvent({type: "input"});
}

function options(page) {
    return popover(page).querySelectorAll(".face-pop-option").map(function (node) {
        return (node.classList.contains("is-active") ? "* " : "") +
            node.querySelector(".face-pop-name").textContent;
    });
}

function opened(page) {
    return waitFor(function () {
        var node = popover(page);
        return node && node.querySelectorAll(".face-pop-option").length > 0;
    });
}

test("a face box is a button that opens the popover (not a drag)", function () {
    var page = pictureWithFaces();
    assert.deepStrictEqual(boxes(page), ["301:Ada Lind", "302:Person 12", "303:?"]);
    var unknown = box(page, 303);
    assert.strictEqual(unknown.getAttribute("role"), "button");
    assert.strictEqual(unknown.getAttribute("tabindex"), "0");
    assert.strictEqual(unknown.getAttribute("aria-haspopup"), "dialog");
    assert.ok(/nobody known: say who it is/.test(unknown.getAttribute("aria-label")));
    assert.ok(/Face of Ada Lind/.test(box(page, 301).getAttribute("aria-label")));
    // A drag that starts on a face moves the picture: no popover.
    unknown.dispatchEvent({type: "pointerdown", pointerId: 1, clientX: 100, clientY: 100,
                           pointerType: "mouse", button: 0});
    page.stage.box.dispatchEvent({type: "pointermove", pointerId: 1, clientX: 160,
                                  clientY: 110});
    page.stage.box.dispatchEvent({type: "pointerup", pointerId: 1, clientX: 160, clientY: 110});
    unknown.dispatchEvent({type: "click", button: 0});
    assert.strictEqual(popover(page), null);
    // A pinch neither.
    unknown.dispatchEvent({type: "pointerdown", pointerId: 2, clientX: 100, clientY: 100,
                           pointerType: "touch"});
    page.stage.box.dispatchEvent({type: "pointerdown", pointerId: 3, clientX: 300,
                                  clientY: 300, pointerType: "touch"});
    page.stage.box.dispatchEvent({type: "pointerup", pointerId: 2, clientX: 101, clientY: 100});
    assert.strictEqual(popover(page), null);
    // A tap does (its end comes to the stage, which holds the pointer), a moment later
    // (a second tap would make it a double tap).
    unknown.dispatchEvent({type: "pointerdown", pointerId: 4, clientX: 100, clientY: 100,
                           pointerType: "touch"});
    page.stage.box.dispatchEvent({type: "pointerup", pointerId: 4, clientX: 102, clientY: 101});
    assert.strictEqual(popover(page), null);
    // (The click that follows the tap opens no second one.)
    unknown.dispatchEvent({type: "click", button: 0, detail: 1});
    return waitFor(function () {
        return popover(page);
    }).then(function (node) {
        assert.strictEqual(node.tagName, "DIALOG");
        assert.strictEqual(node.querySelector(".face-pop-title").textContent, "Who is this?");
        assert.strictEqual(node.querySelector(".face-pop-now").textContent, "In no person yet");
        assert.strictEqual(page.document.activeElement, node.querySelector(".face-pop-input"));
        assert.strictEqual(unknown.getAttribute("aria-expanded"), "true");
        return settle(50);
    }).then(function () {
        assert.strictEqual(page.document.querySelectorAll(".face-popover").length, 1);
        return opened(page);
    }).then(function () {
        // Nothing typed: the named people, none taken by Enter.
        assert.deepStrictEqual(options(page), ["Ada Lind", "Bram", "Dot", "Adele"]);
        assert.ok(/hidden/.test(popover(page).querySelectorAll(".face-pop-option")[2]
            .textContent));
        var input = popover(page).querySelector(".face-pop-input");
        key(input, "Enter");
        assert.strictEqual(page.log.sent.length, 0);
        assert.strictEqual(input.getAttribute("role"), "combobox");
        // The arrows choose.
        key(input, "ArrowDown");
        key(input, "ArrowDown");
        assert.deepStrictEqual(options(page), ["Ada Lind", "* Bram", "Dot", "Adele"]);
        assert.strictEqual(input.getAttribute("aria-activedescendant"),
                           popover(page).querySelectorAll(".face-pop-option")[1].id);
        key(input, "ArrowUp");
        key(input, "ArrowUp");
        assert.deepStrictEqual(options(page), ["Ada Lind", "Bram", "Dot", "* Adele"]);
        // Esc closes it and gives the keyboard back to its box.
        var event = key(input, "Escape");
        assert.ok(event.defaultPrevented);
        assert.strictEqual(popover(page), null);
        assert.strictEqual(page.document.activeElement, box(page, 303));
        assert.strictEqual(box(page, 303).getAttribute("aria-expanded"), "false");
        // Enter (or Space) on a box opens it.
        key(box(page, 302), "Enter");
        assert.ok(popover(page));
        assert.ok(/Now: Person 12/.test(popover(page).querySelector(".face-pop-now")
            .textContent));
    });
});

test("the end of the tap that opened it does nothing in the popover", function () {
    var page = pictureWithFaces();
    var unknown = box(page, 303);
    unknown.dispatchEvent({type: "pointerdown", pointerId: 7, clientX: 100, clientY: 100,
                           pointerType: "touch"});
    page.stage.box.dispatchEvent({type: "pointerup", pointerId: 7, clientX: 100, clientY: 100});
    // (Its touchend is cancelled: no mouse events nor click follow.)
    var end = {type: "touchend", cancelable: true};
    unknown.dispatchEvent(end);
    assert.ok(end.defaultPrevented);
    return opened(page).then(function () {
        var input = popover(page).querySelector(".face-pop-input");
        type(page, "ad");
        var option = popover(page).querySelectorAll(".face-pop-option")[0];
        // A click (and its mousedown) where the finger was, now on the popover: nothing.
        var down = {type: "mousedown", button: 0};
        option.dispatchEvent(down);
        assert.ok(down.defaultPrevented);
        option.dispatchEvent({type: "click", button: 0, detail: 1});
        popover(page).querySelector(".face-pop-actions button").dispatchEvent(
            {type: "click", button: 0, detail: 1});
        assert.strictEqual(page.log.sent.length, 0);
        assert.strictEqual(page.document.activeElement, input);
        // A press that starts there does.
        option.dispatchEvent({type: "pointerdown", pointerId: 8, pointerType: "touch"});
        option.dispatchEvent({type: "click", button: 0, detail: 1});
        return waitFor(function () {
            return page.log.sent.length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0][1], {action: "move", faces: [303], to: 5,
                                                     undo: true});
    });
});

test("a few letters and Enter: the face goes to that person, and Undo", function () {
    var page = pictureWithFaces({reply: function (path, body, state) {
        if (path === "/people/undo") {
            state.people = {names: [{id: 5, label: "Ada Lind", named: true}], faces: faces()};
            return response(200, {undone: true});
        }
        // The face of nobody is Ada Lind's now.
        state.people = {names: [{id: 5, label: "Ada Lind", named: true}],
                        faces: faces().map(function (face) {
                            if (face.id === 303) {
                                face.person = 5;
                                face.label = "Ada Lind";
                            }
                            return face;
                        })};
        return response(200, {person: {id: 5, label: "Ada Lind", faces: 10},
                              undo: "token-7"});
    }});
    var searches = 0;
    var loader = page.context.SITE.peopleForSearch(function () {
        searches += 1;
    }, true);
    loader.want("person:Ada");
    key(box(page, 303), "Enter");
    return opened(page).then(function () {
        assert.strictEqual(searches, 1);
        type(page, "ad");
        assert.deepStrictEqual(options(page), ["* Ada Lind", "Adele",
                                               "A new person \u201cad\u201d"]);
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return boxes(page)[2] === "303:Ada Lind";
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0], ["/api/admin/people/faces", {
            action: "move", faces: [303], to: 5, undo: true}]);
        assert.strictEqual(popover(page), null);
        // The pane's People line follows (from the picture's record, asked for again).
        assert.ok(page.log.requests.indexOf("/api/image/4") >= 0);
        assert.strictEqual(page.info.querySelector(".info-people").textContent.indexOf("Ada Lind"),
                           0);
        // The keyboard is back on the face.
        assert.strictEqual(page.document.activeElement, box(page, 303));
        // The message, with Undo.
        var message = page.document.querySelector(".toast");
        assert.strictEqual(message.querySelector(".toast-text").textContent,
                           "This face is Ada Lind now.");
        // The search pages ask for the people again when a search needs them.
        assert.strictEqual(loader.want("person:Ada"), true);
        return waitFor(function () {
            return searches === 2;
        });
    }).then(function () {
        page.document.querySelector(".toast .toast-action").click();
        return waitFor(function () {
            return boxes(page)[2] === "303:?";
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/api/admin/people/undo", {undo: "token-7"}]);
        assert.strictEqual(page.document.querySelectorAll(".toast .toast-action").length, 0);
        assert.ok(/Undone/.test(page.document.querySelector(".toast").textContent));
    });
});

test("a new name: the unnamed person's (all its faces) or this face's alone", function () {
    var page = pictureWithFaces();
    key(box(page, 302), "Enter");
    return opened(page).then(function () {
        type(page, "Nell");
        assert.deepStrictEqual(options(page), ["* Name Person 12 \u201cNell\u201d",
                                               "Just this face: a new person \u201cNell\u201d"]);
        assert.strictEqual(popover(page).querySelectorAll(".face-pop-option small")[0]
            .textContent, "all 37 faces");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return page.log.sent.length === 1 && !popover(page);
        });
    }).then(function () {
        // (With the face: the server refuses when the face left person 12 meanwhile.)
        assert.deepStrictEqual(page.log.sent[0], ["/api/admin/people/12",
                                                  {name: "Nell", face: 302, undo: true}]);
        key(box(page, 302), "Enter");
        return opened(page);
    }).then(function () {
        type(page, "Nell");
        // (A click takes a choice too.)
        popover(page).querySelectorAll(".face-pop-option")[1].click();
        return waitFor(function () {
            return page.log.sent.length === 2;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/api/admin/people/faces", {
            action: "move", faces: [302], to: null, name: "Nell", undo: true, person: 12}]);
        // A face of nobody: a new person of that name.
        return waitFor(function () {
            return !popover(page);
        });
    }).then(function () {
        key(box(page, 303), "Enter");
        return opened(page);
    }).then(function () {
        type(page, "Nell");
        assert.deepStrictEqual(options(page), ["* A new person \u201cNell\u201d"]);
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return page.log.sent.length === 3;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[2], ["/api/admin/people/faces", {
            action: "move", faces: [303], to: null, name: "Nell", undo: true}]);
    });
});

test("Not <name> and Not a face", function () {
    var page = pictureWithFaces({reply: function () {
        return response(200, {removed: 1, person: null, source: null, changed: 1,
                              undo: "token-9"});
    }});
    key(box(page, 301), "Enter");
    return opened(page).then(function () {
        var buttons = popover(page).querySelectorAll(".face-pop-actions button");
        assert.deepStrictEqual(buttons.map(function (node) {
            return node.textContent;
        }), ["Not Ada Lind", "Not a face"]);
        var link = popover(page).querySelector(".face-pop-now a");
        assert.strictEqual(link.href, "/people?person=5");
        assert.ok(/9 faces/.test(popover(page).querySelector(".face-pop-now").textContent));
        buttons[0].click();
        return waitFor(function () {
            return page.log.sent.length === 1 && !popover(page);
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0], ["/api/admin/people/faces", {
            action: "reject", faces: [301], person: 5, undo: true}]);
        assert.ok(/not Ada Lind any more/.test(page.document.querySelector(".toast-text")
            .textContent));
        key(box(page, 303), "Enter");
        return opened(page);
    }).then(function () {
        popover(page).querySelector(".face-pop-actions button").click();
        return waitFor(function () {
            return page.log.sent.length === 2 && !popover(page);
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/api/admin/people/faces", {
            action: "not_face", faces: [303], undo: true}]);
        // Only the last change can be undone: one message with Undo, the last one.
        var messages = page.document.querySelectorAll(".toast");
        assert.strictEqual(page.document.querySelectorAll(".toast .toast-action").length, 1);
        assert.strictEqual(messages[messages.length - 1].querySelector(".toast-text")
            .textContent, "Marked as not a face.");
        assert.ok(messages[messages.length - 1].querySelector(".toast-action"));
    });
});

test("a refused name stays in the popover with the server's reason", function () {
    var page = pictureWithFaces({reply: function () {
        return response(409, {error: "Cleo is the name of another person already: merge " +
                                     "the two if they are the same person.", other: 99});
    }});
    key(box(page, 302), "Enter");
    return opened(page).then(function () {
        type(page, "Cleo");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return !popover(page).querySelector(".face-pop-error").hidden;
        });
    }).then(function () {
        var error = popover(page).querySelector(".face-pop-error");
        assert.ok(/Cleo is the name of another person/.test(error.textContent));
        assert.strictEqual(error.getAttribute("role"), "alert");
        // (No error message of the page's own: the popover says it.)
        assert.strictEqual(page.document.querySelectorAll(".toast-error").length, 0);
        assert.strictEqual(page.document.activeElement,
                           popover(page).querySelector(".face-pop-input"));
    });
});

test("Ctrl+Z undoes the last change while its message shows", function () {
    var page = pictureWithFaces();
    key(box(page, 303), "Enter");
    return opened(page).then(function () {
        type(page, "br");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return page.log.sent.length === 1 && page.document.querySelector(".toast");
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0][1].to, 6);
        // (Not in a field: there it is the field's. The page listens on the document.)
        var field = page.document.createElement("input");
        page.document.body.appendChild(field);
        page.document.dispatchEvent({type: "keydown", key: "z", ctrlKey: true, target: field});
        assert.strictEqual(page.log.sent.length, 1);
        var event = {type: "keydown", key: "z", ctrlKey: true, target: page.document.body};
        page.document.dispatchEvent(event);
        assert.ok(event.defaultPrevented);
        return waitFor(function () {
            return page.log.sent.length === 2;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[1], ["/api/admin/people/undo",
                                                  {undo: "token-1"}]);
        // Once.
        page.document.dispatchEvent({type: "keydown", key: "z", ctrlKey: true,
                                     target: page.document.body});
        return settle(20);
    }).then(function () {
        assert.strictEqual(page.log.sent.length, 2);
    });
});

test("a video's faces are in the pane: a press moves the video there and asks who",
     function () {
         var people = {names: [{id: 5, label: "Ada Lind", named: true, at: [3.5]}],
                       faces: [{id: 401, x: 0.2, y: 0.3, w: 0.1, h: 0.1, person: 5,
                                label: "Ada Lind", quality: 0.9, at: 3.5},
                               {id: 402, x: 0.6, y: 0.3, w: 0.1, h: 0.1, person: null,
                                label: "", quality: 0.4, at: 72.25}]};
         var page = pictureWithFaces({rec: {i: 4, l: "lib", t: "Party", mv: 1, dr: 90},
                                      mode: "video", people: people});
         // (No boxes over a video.)
         assert.deepStrictEqual(boxes(page), []);
         var strip = page.info.querySelectorAll(".info-face");
         assert.deepStrictEqual(strip.map(function (node) {
             return node.textContent;
         }), ["Ada Lind0:03", "?1:12"]);
         assert.ok(/nobody known: say who it is, at 1:12/.test(strip[1]
             .getAttribute("aria-label")));
         assert.strictEqual(strip[1].querySelector("img").src, "/face/402");
         strip[1].click();
         assert.deepStrictEqual(page.seeks, [72.25]);
         assert.ok(popover(page));
         // "Hide faces": the strip goes.
         page.info.querySelector(".info-faces").click();
         assert.strictEqual(page.info.querySelectorAll(".info-face").length, 0);
         assert.strictEqual(page.info.querySelector(".info-faces").textContent, "Show faces");
         assert.strictEqual(page.document.activeElement, page.info.querySelector(".info-faces"));
         return opened(page);
     });

test("visitors get no boxes and no popover", function () {
    var page = pictureWithFaces({admin: false});
    assert.deepStrictEqual(boxes(page), []);
    assert.strictEqual(page.info.querySelectorAll(".info-faces").length, 0);
    assert.strictEqual(popover(page), null);
});

test("another picture closes the popover", function () {
    var page = pictureWithFaces();
    key(box(page, 303), "Enter");
    assert.ok(popover(page));
    page.context.VIEWER.renderInfo(page.info, {i: 9, l: "lib", t: "Other"}, {id: 9},
                                   {get: function () {
                                       return null;
                                   }, stage: page.stage});
    assert.strictEqual(popover(page), null);
});

test("a message can carry an action (Undo)", function () {
    var page = serverPage(function () {
        return null;
    });
    var ran = 0;
    var message = page.context.SHELL.toast("Done.", "ok", 5, {text: "Undo", title: "Undo it",
                                                              run: function () {
                                                                  ran += 1;
                                                              }});
    var button = message.querySelector(".toast-action");
    assert.strictEqual(button.textContent, "Undo");
    button.click();
    assert.strictEqual(ran, 1);
    assert.strictEqual(message.parentNode, null);
    // (Without one, as before.)
    var plain = page.context.SHELL.toast("Saved.", "ok");
    assert.strictEqual(plain.querySelectorAll(".toast-action").length, 0);
});

test("on a phone the choices and buttons are large", function () {
    var css = read("style.css");
    var found = {};
    var re = /@media \(pointer: coarse\) \{([\s\S]*?)\n\}/g;
    var block;
    while ((block = re.exec(css)) !== null) {
        [".face-pop-option", ".face-pop-actions .button", ".face-pop-x"].forEach(function (name) {
            var rule = new RegExp(name.replace(/\./g, "\\.") + " \\{([^}]*)\\}").exec(block[1]);
            if (rule) {
                found[name] = Number((/min-height: (\d+)px/.exec(rule[1]) || [0, 0])[1]);
            }
        });
        var input = /\.face-pop-input \{([^}]*)\}/.exec(block[1]);
        if (input) {
            // (16 px: phones do not zoom into the field.)
            assert.ok(/font-size: 16px/.test(input[1]), input[1]);
        }
    }
    assert.deepStrictEqual(Object.keys(found).sort(), [".face-pop-actions .button",
                                                       ".face-pop-option", ".face-pop-x"]);
    Object.keys(found).forEach(function (name) {
        assert.ok(found[name] >= 44, name + ": " + found[name]);
    });
});

test("the server's names: accents count, sharp s and case do not", function () {
    var format = require(path.join(STATIC, "viewer.js")).format;
    var people = [{id: 1, name: "Jos\u00e9", label: "Jos\u00e9", faces: 2},
                  {id: 2, name: "Stra\u00dfe", label: "Stra\u00dfe", faces: 1}];
    function kinds(text) {
        return format.faceChoices(people, {person: null}, text).map(function (choice) {
            return choice.kind === "person" ? choice.person.label : choice.kind + ":" +
                choice.name;
        });
    }
    // "Jose" finds the accented name first, and can be a new name of its own.
    assert.deepStrictEqual(kinds("Jose"), ["Jos\u00e9", "new:Jose"]);
    assert.deepStrictEqual(kinds("jos\u00e9"), ["Jos\u00e9"]);
    // "STRASSE" is the name with a sharp s to the server: no new person of it.
    assert.deepStrictEqual(kinds("STRASSE"), []);
    assert.deepStrictEqual(kinds("stra\u00dfe"), ["Stra\u00dfe"]);
});

test("faces are in reading order: row by row, each from the left", function () {
    var format = require(path.join(STATIC, "viewer.js")).format;
    var crowd = [
        {id: 1, x: 0.05, y: 0.20, w: 0.08, h: 0.1}, {id: 2, x: 0.10, y: 0.52, w: 0.08, h: 0.1},
        {id: 3, x: 0.20, y: 0.21, w: 0.08, h: 0.1}, {id: 4, x: 0.25, y: 0.50, w: 0.08, h: 0.1},
        {id: 5, x: 0.35, y: 0.19, w: 0.08, h: 0.1}
    ];
    assert.deepStrictEqual(format.readingOrder(crowd).map(function (face) {
        return face.id;
    }), [1, 3, 5, 2, 4]);
    // Tab reaches the boxes so.
    var people = {names: [], faces: crowd.map(function (face) {
        return {id: 500 + face.id, x: face.x, y: face.y, w: face.w, h: face.h, person: null,
                label: "", quality: 0.8};
    })};
    var page = pictureWithFaces({people: people});
    assert.deepStrictEqual(boxes(page), ["501:?", "503:?", "505:?", "502:?", "504:?"]);
});

test("a double tap or a double click zooms in, and opens nothing", function () {
    var page = pictureWithFaces();
    var unknown = box(page, 303);
    function tap(id, x) {
        unknown.dispatchEvent({type: "pointerdown", pointerId: id, clientX: x, clientY: 100,
                               pointerType: "touch"});
        page.stage.box.dispatchEvent({type: "pointerup", pointerId: id, clientX: x,
                                      clientY: 100});
    }
    tap(11, 100);
    tap(12, 104);
    return settle(450).then(function () {
        assert.ok(!popover(page), "a popover opened");
        // A double click (the stage zooms in on its dblclick).
        [1, 2].forEach(function (count) {
            unknown.dispatchEvent({type: "pointerdown", pointerId: 1, clientX: 100,
                                   clientY: 100, pointerType: "mouse", button: 0});
            page.stage.box.dispatchEvent({type: "pointerup", pointerId: 1, clientX: 100,
                                          clientY: 100});
            unknown.dispatchEvent({type: "click", button: 0, detail: count});
        });
        unknown.dispatchEvent({type: "dblclick", button: 0});
        return settle(450);
    }).then(function () {
        assert.ok(!popover(page), "a popover opened");
        // One tap, then nothing: it opens.
        tap(13, 100);
        return waitFor(function () {
            return popover(page);
        });
    });
});

test("an Undo after moving on leaves the boxes of the picture shown", function () {
    var page = pictureWithFaces();
    key(box(page, 303), "Enter");
    return opened(page).then(function () {
        type(page, "ad");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return page.document.querySelector(".toast .toast-action");
        });
    }).then(function () {
        return settle(50);
    }).then(function () {
        // The viewer moves on to picture 9 (the same stage), with a face of its own.
        var other = {i: 9, l: "lib", t: "Other", w: 1600, hgt: 1200};
        page.stage.rec = other;
        page.context.VIEWER.renderInfo(page.info, other, {id: 9, people: {names: [], faces: [
            {id: 901, x: 0.1, y: 0.1, w: 0.2, h: 0.2, person: null, label: "",
             quality: 0.7}]}}, {get: function () {
                 return null;
             }, stage: page.stage});
        assert.deepStrictEqual(boxes(page), ["901:?"]);
        page.document.querySelector(".toast .toast-action").click();
        return waitFor(function () {
            var messages = page.document.querySelectorAll(".toast");
            return messages.length && /Undone/.test(messages[messages.length - 1].textContent);
        });
    }).then(function () {
        return settle(50);
    }).then(function () {
        assert.deepStrictEqual(boxes(page), ["901:?"]);
    });
});

/* A picture with faces whose /api/people answers only when let go: page.release(). */
function slowPeople() {
    var waiting = [];
    var page = pictureWithFaces();
    var answer = page.context.fetch;
    page.context.fetch = function (url, init) {
        if (String(url) === "/api/people?hidden=1") {
            return new Promise(function (resolve) {
                waiting.push(function () {
                    resolve(answer(url, init));
                });
            });
        }
        return answer(url, init);
    };
    page.release = function () {
        waiting.splice(0).forEach(function (go) {
            go();
        });
    };
    return page;
}

test("Enter while the people are on their way waits for them, and takes a person only",
     function () {
         var page = slowPeople();
         key(box(page, 302), "Enter");
         var input = popover(page).querySelector(".face-pop-input");
         type(page, "ad");
         // (Only new names are known yet: none is taken.)
         assert.ok(/Loading the people/.test(popover(page).querySelector(".face-pop-hint")
             .textContent));
         assert.strictEqual(popover(page).querySelectorAll(".face-pop-option.is-active").length,
                            0);
         key(input, "Enter");
         var other = slowPeople();
         return settle(30).then(function () {
             assert.strictEqual(page.log.sent.length, 0);
             page.release();
             return waitFor(function () {
                 return page.log.sent.length === 1;
             });
         }).then(function () {
             // Ada Lind, whom the letters name -- not person 12 named "ad".
             assert.deepStrictEqual(page.log.sent[0], ["/api/admin/people/faces", {
                 action: "move", faces: [302], to: 5, undo: true, person: 12}]);
             // Letters that name nobody: Enter waits, then takes nothing (a new name is
             // chosen by hand).
             key(box(other, 303), "Enter");
             type(other, "zq");
             key(popover(other).querySelector(".face-pop-input"), "Enter");
             other.release();
             return waitFor(function () {
                 return /A new person/.test(popover(other).textContent) &&
                     popover(other).querySelectorAll(".face-pop-option.is-active").length === 1;
             });
         }).then(function () {
             return settle(30);
         }).then(function () {
             assert.strictEqual(other.log.sent.length, 0);
         });
     });

/* The server of pictureWithFaces, changing face 303 (move: to "to"; undo: back to
   nobody), so the boxes follow. */
function movingFace(path, body, state) {
    var names = [{id: 5, label: "Ada Lind", named: true},
                 {id: 12, label: "Person 12", named: false}];
    if (path === "/people/undo") {
        state.people = {names: names, faces: faces()};
        return response(200, {undone: true});
    }
    var label = body.to === 5 ? "Ada Lind" : "Bram";
    state.people = {names: names, faces: faces().map(function (face) {
        if (face.id === 303) {
            face.person = body.to;
            face.label = label;
        }
        return face;
    })};
    return response(200, {person: {id: body.to, label: label, faces: 10},
                          undo: "token-" + body.to});
}

test("the keyboard stays on the face: after Undo, and on another picture", function () {
    var page = pictureWithFaces({reply: movingFace});
    // (A holder of the stage that takes the keyboard, as the viewer's root does.)
    var holder = page.document.createElement("div");
    holder.setAttribute("tabindex", "-1");
    page.document.body.appendChild(holder);
    holder.appendChild(page.stage.box);
    key(box(page, 303), "Enter");
    return opened(page).then(function () {
        type(page, "ad");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return boxes(page)[2] === "303:Ada Lind";
        });
    }).then(function () {
        assert.ok(page.document.activeElement === box(page, 303), "not on face 303");
        // Ctrl+Z on the face: the face's new box has the keyboard again.
        page.document.dispatchEvent({type: "keydown", key: "z", ctrlKey: true,
                                     target: page.document.activeElement});
        return waitFor(function () {
            return boxes(page)[2] === "303:?";
        });
    }).then(function () {
        assert.ok(page.document.activeElement === box(page, 303), "not on face 303");
        // The message's Undo, clicked (it goes with its message): the face again.
        key(box(page, 303), "Enter");
        return opened(page);
    }).then(function () {
        type(page, "br");
        key(popover(page).querySelector(".face-pop-input"), "Enter");
        return waitFor(function () {
            return page.document.querySelector(".toast .toast-action");
        });
    }).then(function () {
        var undo = page.document.querySelector(".toast .toast-action");
        // Its shortcut, told to screen readers and shown by it (style.css).
        assert.strictEqual(undo.getAttribute("aria-keyshortcuts"), "Control+Z");
        assert.strictEqual(undo.getAttribute("data-keys"), "Ctrl+Z");
        undo.focus();
        undo.click();
        return waitFor(function () {
            var messages = page.document.querySelectorAll(".toast");
            return /Undone/.test(messages[messages.length - 1].textContent);
        });
    }).then(function () {
        return settle(30);
    }).then(function () {
        assert.ok(page.document.activeElement === box(page, 303), "not on face 303");
        // Another picture: its boxes go, and the keyboard to the stage's holder.
        var other = {i: 9, l: "lib", t: "Other", w: 1600, hgt: 1200};
        page.stage.rec = other;
        page.context.VIEWER.renderInfo(page.info, other, {id: 9}, {get: function () {
            return null;
        }, stage: page.stage});
        assert.deepStrictEqual(boxes(page), []);
        assert.ok(page.document.activeElement === holder, "not on the holder");
    });
});

test("a face box out of sight comes into sight; the stage is never scrolled", function () {
    var page = pictureWithFaces();
    var stage = page.stage;
    var placed = 0;
    // The picture zoomed in: face 303 is off the stage's right side.
    stage.tx = -600;
    stage.ty = 0;
    stage.base = {w: 1000, h: 750};
    stage.place = function () {
        placed += 1;
    };
    stage.box.getBoundingClientRect = function () {
        return {left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600};
    };
    var target = box(page, 303);
    target.getBoundingClientRect = function () {
        return {left: 900, top: 200, right: 960, bottom: 260, width: 60, height: 60};
    };
    target.dispatchEvent({type: "focus"});
    return settle(20).then(function () {
        // (Moved left by the stage's own place: its right side 8 px in.)
        assert.strictEqual(stage.tx, -600 - (960 - 792));
        assert.ok(placed >= 1);
        // A scroll of the stage (the browser's, to show a box) is taken back.
        stage.box.scrollLeft = 940;
        stage.box.scrollTop = 0;
        stage.box.dispatchEvent({type: "scroll"});
        assert.strictEqual(stage.box.scrollLeft, 0);
    });
});

test("a 360 photo turns to the face chosen in the pane", function () {
    var people = {names: [{id: 5, label: "Ada Lind", named: true}],
                  faces: [{id: 601, x: 0.70, y: 0.40, w: 0.04, h: 0.06, person: 5,
                           label: "Ada Lind", quality: 0.9}]};
    var rec = {i: 4, l: "lib", t: "Hill", p: "360", w: 4096, hgt: 2048};
    var page = pictureWithFaces({rec: rec, mode: "pano", people: people});
    var looked = [];
    page.stage.lookAt = function (u, v) {
        looked.push([Math.round(u * 100) / 100, Math.round(v * 100) / 100]);
        return true;
    };
    page.info.querySelector(".info-face").click();
    assert.deepStrictEqual(looked, [[0.72, 0.43]]);
    assert.ok(popover(page));
    // The view looks there: the middle of the screen is that point of the panorama.
    var VIEWER = page.context.VIEWER;
    var fake = {mode: "pano", view: {yaw: 0, pitch: 0, fov: 1.2}, redraw: function () {},
                state: function () {}};
    assert.strictEqual(VIEWER.Stage.prototype.lookAt.call(fake, 0.72, 0.43), true);
    var middle = VIEWER.format.panoLookup(fake.view.yaw, fake.view.pitch, 0, 0, {x: 1, y: 1});
    assert.ok(Math.abs(middle.u - 0.72) < 1e-6 && Math.abs(middle.v - 0.43) < 1e-6,
              JSON.stringify(middle));
    assert.strictEqual(fake.view.fov, 1.2);
    assert.strictEqual(VIEWER.Stage.prototype.lookAt.call({mode: "image"}, 0.5, 0.5), false);
});

test("faces in a crowd show their names on focus only", function () {
    var people = {names: [{id: 5, label: "Ada Lind", named: true}], faces: [
        {id: 701, x: 0.10, y: 0.3, w: 0.02, h: 0.1, person: 5, label: "Ada Lind",
         quality: 0.9},
        {id: 702, x: 0.13, y: 0.3, w: 0.02, h: 0.1, person: null, label: "", quality: 0.8},
        {id: 703, x: 0.60, y: 0.3, w: 0.02, h: 0.1, person: null, label: "", quality: 0.8}]};
    var page = pictureWithFaces({people: people});
    // (The picture is 1000 px wide here: "Ada Lind" under face 701 would cover face 702.)
    assert.ok(box(page, 701).classList.contains("is-tight"));
    assert.ok(!box(page, 702).classList.contains("is-tight"));
    assert.ok(!box(page, 703).classList.contains("is-tight"));
    var css = read("style.css");
    assert.ok(/\.face-box\.is-tight \.face-name \{\s*display: none;/.test(css));
    // To a finger, a small face takes a tap around it, under the other boxes.
    var coarse = /@media \(pointer: coarse\) \{\s*\.face-box::before \{([^}]*)\}/.exec(css);
    assert.ok(coarse, "no tap area");
    assert.ok(/min-width: 44px/.test(coarse[1]) && /z-index: -1/.test(coarse[1]));
});

test("the popover: units, a list that can be empty, by its face, errors said", function () {
    var page = pictureWithFaces({reply: function () {
        return new Promise(function (resolve) {
            setTimeout(function () {
                resolve(response(409, {error: "Ada Lind is busy."}));
            }, 60);
        });
    }});
    // (The popover as tall as its list; the face's box low on the screen: it opens above.)
    var create = page.document.createElement;
    page.document.createElement = function (tag) {
        var node = create.apply(page.document, arguments);
        if (String(tag).toLowerCase() === "dialog") {
            node.getBoundingClientRect = function () {
                var height = 120 + 40 * node.querySelectorAll(".face-pop-option").length;
                return {left: 0, top: 0, right: 340, bottom: height, width: 340, height: height};
            };
        }
        return node;
    };
    var low = box(page, 302);
    low.getBoundingClientRect = function () {
        return {left: 500, top: 700, right: 560, bottom: 760, width: 60, height: 60};
    };
    key(low, "Enter");
    return opened(page).then(function () {
        var node = popover(page);
        // Faces, as the head counts them.
        assert.strictEqual(node.querySelectorAll(".face-pop-option small")[0].textContent,
                           "9 faces");
        var input = node.querySelector(".face-pop-input");
        assert.strictEqual(input.getAttribute("aria-expanded"), "true");
        // Above, held by its foot (900 - 694 px): it stays by the face as the list
        // shrinks.
        assert.strictEqual(node.style.top, "auto");
        assert.strictEqual(node.style.bottom, "206px");
        type(page, "person 7");
        assert.strictEqual(node.querySelectorAll(".face-pop-option").length, 0);
        assert.strictEqual(input.getAttribute("aria-expanded"), "false");
        assert.strictEqual(node.style.bottom, "206px");
        type(page, "br");
        key(input, "Enter");
        // Closed before the answer: its refusal is said all the same.
        key(input, "Escape");
        return waitFor(function () {
            var errors = page.document.querySelectorAll(".toast-error");
            return errors.length && /Ada Lind is busy/.test(errors[0].textContent);
        });
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
