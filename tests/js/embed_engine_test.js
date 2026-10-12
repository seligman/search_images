/* Tests for static/embed-engine.js: the engine that embeds queries in an
   exported page. The models are the made-up ones of tests/fake_gguf.py,
   converted by browser_model.py; the numbers to match are llama.cpp's for
   the same models (tests/fixtures/fake_bert_llamacpp.json).

   Run by tests/test_browser_model.py:
       node tests/js/embed_engine_test.js MODELS_FOLDER EXPECT.json
   MODELS_FOLDER holds mean/, cls/, f16/ and dense/ (each with meaning/model.js
   and meaning/w-NNN.js); EXPECT.json {fixture, prefix, dense: {rows, bias},
   ties: [{values, int8}]}. Prints one line per test and exits with status 1
   when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = path.join(__dirname, "..", "..", "static");
var folder = process.argv[2];
var expect = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var fixture = JSON.parse(fs.readFileSync(expect.fixture, "utf8"));

// The kernel defines EMBED_KERNEL, as its <script> does in a page.
vm.runInThisContext(fs.readFileSync(path.join(STATIC, "embed-kernel.js"), "utf8"));
var engineModule = require(path.join(STATIC, "embed-engine.js"));
var EMBED = engineModule.EMBED;
global.SEARCH_EMBED = engineModule.SEARCH_EMBED;

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function modelOf(name) {
    var text = fs.readFileSync(path.join(folder, name, "meaning", "model.js"), "utf8");
    return JSON.parse(text.slice("SEARCH_EMBED.model(".length, -3));
}

function manifestOf(name) {
    var model = modelOf(name);
    var parts = [];
    for (var k = 0; k < model.parts; k++) {
        parts.push("meaning/w-" + ("00" + k).slice(-3) + ".js");
    }
    return {id: model.id, model: "meaning/model.js", parts: parts};
}

// Runs a model file of a folder as its <script> would (asynchronously).
function loader(name, log) {
    return function (url) {
        if (log) {
            log.push(url);
        }
        return new Promise(function (resolve) {
            setImmediate(function () {
                vm.runInThisContext(fs.readFileSync(path.join(folder, name, url), "utf8"));
                resolve();
            });
        });
    };
}

function load(name, options) {
    options = options || {};
    return EMBED.load(manifestOf(name), {load: options.load || loader(name), wasm: options.wasm,
                                         retryDelay: 0, progress: options.progress});
}

function unit(values) {
    var norm = 0;
    var k;
    for (k = 0; k < values.length; k++) {
        norm += values[k] * values[k];
    }
    norm = Math.sqrt(norm);
    var out = [];
    for (k = 0; k < values.length; k++) {
        out.push(values[k] / norm);
    }
    return out;
}

function cosine(a, b) {
    var dot = 0;
    var na = 0;
    var nb = 0;
    for (var k = 0; k < a.length; k++) {
        dot += a[k] * b[k];
        na += a[k] * a[k];
        nb += b[k] * b[k];
    }
    return dot / Math.sqrt(na * nb);
}

function sameBytes(a, b) {
    return Buffer.from(a.buffer, a.byteOffset, a.byteLength)
        .equals(Buffer.from(b.buffer, b.byteOffset, b.byteLength));
}

/* ---------- Tokens ------------------------------------------------------------- */

test("words are split, lower-cased and stripped of accents as llama.cpp does", function () {
    var config = {lowercase: true, strip_accents: true};
    assert.deepStrictEqual(EMBED.words("Caf\u00e9 CR\u00c8ME, \u00df!", config),
                           ["cafe", "creme", ",", "\u00df", "!"]);
    // CJK characters stand alone; emoji and other symbols stay in words;
    // control and format characters go; NEL is a space, a zero-width
    // no-break space is not.
    assert.deepStrictEqual(EMBED.words("\u65e5\u672cx \ud83c\udf89 a\u0007b c\u0085d e\ufefff",
                                       config),
                           ["\u65e5", "\u672c", "x", "\ud83c\udf89", "ab", "c", "d", "ef"]);
    assert.deepStrictEqual(EMBED.words("$1,234.56 a+b <c>", config),
                           ["$", "1", ",", "234", ".", "56", "a", "+", "b", "<", "c", ">"]);
    assert.deepStrictEqual(EMBED.words("\u0130stanbul na\u00efve", config),
                           ["istanbul", "naive"]);
    assert.deepStrictEqual(EMBED.words("Caf\u00e9", {lowercase: false, strip_accents: false}),
                           ["Caf\u00e9"]);
});

test("token ids are llama.cpp's, cut to the model's length", function () {
    var tokenize = EMBED.tokenizer(modelOf("mean"));
    var room = modelOf("mean").max_tokens;
    var checked = 0;
    fixture.models.mean.forEach(function (entry) {
        var expected = entry.ids.length > room
            ? entry.ids.slice(0, room - 1).concat([entry.ids[entry.ids.length - 1]]) : entry.ids;
        assert.deepStrictEqual(tokenize(entry.text), expected, JSON.stringify(entry.text));
        checked += 1;
    });
    assert.ok(checked >= 12);
    // Over the length: [CLS], the first pieces, [SEP].
    var long = tokenize(new Array(200).join("dog "));
    assert.strictEqual(long.length, room);
    assert.strictEqual(long[0], 2);
    assert.strictEqual(long[room - 1], 3);
    // A word made of nothing in the vocabulary is one unknown token.
    assert.deepStrictEqual(tokenize("\u00e6\u00e6"), [2, 1, 3]);
    // The special tokens' own texts are those tokens, as llama.cpp's server
    // reads them (exactly so: "[mask]" is words), also inside a word.
    assert.deepStrictEqual(tokenize("dog[MASK]cat"), [2, 7, 4, 8, 3]);
    assert.deepStrictEqual(tokenize("[MASK][MASK]"), [2, 4, 4, 3]);
    assert.ok(fixture.models.mean.some(function (entry) {
        return entry.text.indexOf("[MASK]") >= 0;
    }));
});

/* ---------- Vectors -------------------------------------------------------------- */

function againstLlama(name, pooling, minimum) {
    return load(name).then(function (engine) {
        var checked = 0;
        fixture.models[pooling].forEach(function (entry) {
            if (!entry.vec) {
                return;
            }
            var made = engine.document(entry.text);
            var value = cosine(made, unit(entry.vec));
            assert.ok(value >= minimum, name + " " + JSON.stringify(entry.text) + ": " + value);
            checked += 1;
        });
        assert.ok(checked >= 10);
    });
}

test("vectors match llama.cpp's (mean pooling)", function () {
    return againstLlama("mean", "mean", 0.9999);
});

test("vectors match llama.cpp's (CLS pooling)", function () {
    return againstLlama("cls", "cls", 0.9999);
});

test("F16 matrices converted to Q8_0 stay close to llama.cpp's", function () {
    return againstLlama("f16", "mean", 0.999);
});

test("the WebAssembly and JavaScript paths give the same bits", function () {
    if (!EMBED.kernel()) {
        console.log("     (no WebAssembly SIMD here: only the JavaScript path ran)");
        return null;
    }
    return Promise.all([load("mean"), load("mean", {wasm: false}), load("dense"),
                        load("dense", {wasm: false})]).then(function (engines) {
        assert.strictEqual(engines[0].backend, "wasm-simd");
        assert.strictEqual(engines[1].backend, "js");
        fixture.models.mean.forEach(function (entry) {
            assert.ok(sameBytes(engines[0].document(entry.text), engines[1].document(entry.text)),
                      JSON.stringify(entry.text));
            assert.ok(sameBytes(engines[2].query(entry.text), engines[3].query(entry.text)),
                      JSON.stringify(entry.text));
        });
    });
});

test("the Dense head is applied (its first dims rows)", function () {
    return load("dense").then(function (engine) {
        assert.strictEqual(engine.model.dims, 16);
        fixture.models.mean.forEach(function (entry) {
            if (!entry.vec) {
                return;
            }
            var expected = expect.dense.rows.map(function (row, r) {
                var sum = 0;
                for (var k = 0; k < row.length; k++) {
                    sum += row[k] * entry.vec[k];
                }
                return sum + expect.dense.bias[r];
            });
            var made = engine.document(entry.text);
            assert.strictEqual(made.length, 16);
            var value = cosine(made, expected);
            assert.ok(value >= 0.9999, JSON.stringify(entry.text) + ": " + value);
        });
    });
});

test("queries get the prefix and are stored like the server's vectors", function () {
    return load("dense").then(function (engine) {
        var query = engine.query("a dog on the beach");
        assert.ok(query instanceof Int8Array);
        assert.strictEqual(query.length, 16);
        var direct = EMBED.quantize(engine.document(expect.prefix + "a dog on the beach"));
        for (var k = 0; k < 16; k++) {
            assert.ok(Math.abs(query[k] - direct[k]) <= 1);
        }
        var norm = 0;
        for (k = 0; k < 16; k++) {
            norm += query[k] * query[k];
        }
        assert.ok(Math.abs(Math.sqrt(norm) - 127) < 2);
    });
});

test("rounding ties go to the even number, as in Python", function () {
    expect.ties.forEach(function (entry) {
        assert.deepStrictEqual(Array.prototype.slice.call(EMBED.quantize(entry.values)),
                               entry.int8, JSON.stringify(entry.values));
    });
    assert.strictEqual(EMBED.halfToFloat(EMBED.floatToHalf(1 / 3)), 0.333251953125);
    assert.strictEqual(EMBED.floatToHalf(65520), 0x7C00);
    assert.strictEqual(EMBED.floatToHalf(-2.9802322387695312e-8), 0x8000);
    assert.strictEqual(EMBED.floatToHalf(5.960464477539063e-8), 1);
});

test("the self-check compares the probe texts with the server's vectors", function () {
    return load("mean").then(function (engine) {
        var model = modelOf("mean");
        var bytes = fs.readFileSync(path.join(folder, "mean", "meaning", "w-000.js"), "utf8");
        var blob = new Uint8Array(Buffer.from(bytes.split("\"")[1], "base64"));
        function encode(vector) {
            return Buffer.from(vector.buffer, vector.byteOffset, vector.length).toString("base64");
        }
        model.probe = [{text: "a dog on the beach", vec: encode(EMBED.quantize(
            engine.document("a dog on the beach")))}];
        assert.ok(EMBED.create(model, blob).check() > 0.999);
        model.probe = [{text: "a dog on the beach", vec: encode(EMBED.quantize(
            engine.document("the red car, the red house.")))}];
        assert.ok(EMBED.create(model, blob).check() < 0.99);
        model.probe = [];
        assert.strictEqual(EMBED.create(model, blob).check(), -1);
    });
});

/* ---------- Loading -------------------------------------------------------------- */

test("parts are added one at a time and may arrive in any order", function () {
    var log = [];
    var progress = [];
    return load("dense", {load: loader("dense", log), progress: function (loaded, total) {
        progress.push([loaded, total]);
    }}).then(function (inOrder) {
        assert.deepStrictEqual(log, ["meaning/model.js", "meaning/w-000.js"]);
        assert.strictEqual(progress[0][0], 0);
        assert.strictEqual(progress[progress.length - 1][0], progress[0][1]);
        // A loader that runs every part at the first request, last first.
        var manifest = manifestOf("mean");
        var ran = false;
        var model = modelOf("mean");
        var text = fs.readFileSync(path.join(folder, "mean", "meaning", "w-000.js"), "utf8");
        var data = text.split("\"")[1];
        var size = Math.ceil(model.bytes / 3);
        var pieces = [];
        var sums = [];
        for (var k = 0; k < 3; k++) {
            var chunk = Buffer.from(data, "base64").subarray(k * size, (k + 1) * size);
            pieces.push("SEARCH_EMBED.part(" + k + ",\"" + chunk.toString("base64") + "\");");
            sums.push(EMBED.crc32(chunk));
        }
        manifest.parts = ["meaning/w-000.js", "meaning/w-001.js", "meaning/w-002.js"];
        var modelText = "SEARCH_EMBED.model(" + JSON.stringify(Object.assign({}, model, {
            parts: 3, part_bytes: size, sums: sums})) + ");";
        return EMBED.load(manifest, {retryDelay: 0, load: function (url) {
            return new Promise(function (resolve) {
                setImmediate(function () {
                    if (url === "meaning/model.js") {
                        vm.runInThisContext(modelText);
                    } else if (!ran) {
                        ran = true;
                        pieces.slice().reverse().forEach(function (piece) {
                            vm.runInThisContext(piece);
                        });
                    }
                    resolve();
                });
            });
        }}).then(function (anyOrder) {
            return load("mean").then(function (reference) {
                fixture.models.mean.slice(0, 4).forEach(function (entry) {
                    assert.ok(sameBytes(anyOrder.document(entry.text),
                                        reference.document(entry.text)));
                });
                assert.ok(inOrder.query("dog").length === 16);
            });
        });
    });
});

test("a missing part is asked for twice, then the engine is not made", function () {
    var asked = [];
    return EMBED.load(manifestOf("mean"), {retryDelay: 0, load: function (url) {
        asked.push(url);
        if (url !== "meaning/model.js") {
            return Promise.reject(new Error("404"));
        }
        return loader("mean")(url);
    }}).then(function () {
        assert.fail("loaded");
    }, function (error) {
        assert.ok(error instanceof Error);
        assert.deepStrictEqual(asked, ["meaning/model.js", "meaning/w-000.js", "meaning/w-000.js"]);
    });
});

test("a damaged part, or another export's model, is refused", function () {
    var text = fs.readFileSync(path.join(folder, "mean", "meaning", "w-000.js"), "utf8");
    function withPart(part) {
        return EMBED.load(manifestOf("mean"), {retryDelay: 0, load: function (url) {
            if (url === "meaning/model.js") {
                return loader("mean")(url);
            }
            return new Promise(function (resolve) {
                setImmediate(function () {
                    vm.runInThisContext(part);
                    resolve();
                });
            });
        }}).then(function () {
            assert.fail("loaded");
        }, function (error) {
            assert.ok(/damaged/.test(error.message), error.message);
        });
    }
    // Cut short; or as long as it should be, but with other bytes in it
    // (its CRC-32 is not the one in model.js).
    var start = text.indexOf("\"") + 1;
    var damaged = Promise.all([
        withPart(text.slice(0, text.length - 40) + "\");"),
        withPart(text.slice(0, start + 400) + new Array(401).join("A") + text.slice(start + 800))
    ]);
    var manifest = manifestOf("mean");
    manifest.id = "m-000000000000";
    var other = EMBED.load(manifest, {retryDelay: 0, load: loader("mean")}).then(function () {
        assert.fail("loaded");
    }, function (error) {
        assert.ok(/not this export's/.test(error.message), error.message);
    });
    return Promise.all([damaged, other]);
});

test("the CRC-32 of the parts is zlib's", function () {
    assert.strictEqual(EMBED.crc32(Buffer.from("123456789", "ascii")), 0xCBF43926);
    assert.strictEqual(EMBED.crc32(new Uint8Array(0)), 0);
    var model = modelOf("mean");
    assert.strictEqual(model.sums.length, model.parts);
    var text = fs.readFileSync(path.join(folder, "mean", "meaning", "w-000.js"), "utf8");
    assert.strictEqual(EMBED.crc32(Buffer.from(text.split("\"")[1], "base64")), model.sums[0]);
});

test("this Node.js can run the engine", function () {
    assert.strictEqual(EMBED.unsupported(), "");
});

/* ---------- Run ------------------------------------------------------------------ */

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
