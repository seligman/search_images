/* Embeds texts as documents with a converted model (browser_model.py) the way
   an exported page does, and prints [{text, vec (base64 of the stored signed
   bytes)}] as JSON: the probe of tests that need the page's self-check to
   pass (tests/test_meaning_export.py).

       node tests/js/embed_text.js FOLDER TEXT...
   FOLDER holds meaning/model.js and meaning/w-NNN.js.
*/
"use strict";

var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = path.join(__dirname, "..", "..", "static");
var folder = process.argv[2];
var texts = process.argv.slice(3);

vm.runInThisContext(fs.readFileSync(path.join(STATIC, "embed-kernel.js"), "utf8"));
var engineModule = require(path.join(STATIC, "embed-engine.js"));
global.SEARCH_EMBED = engineModule.SEARCH_EMBED;

var model = JSON.parse(fs.readFileSync(path.join(folder, "meaning", "model.js"), "utf8")
    .slice("SEARCH_EMBED.model(".length, -3));
var parts = [];
for (var k = 0; k < model.parts; k++) {
    parts.push("meaning/w-" + ("00" + k).slice(-3) + ".js");
}
engineModule.EMBED.load({id: model.id, model: "meaning/model.js", parts: parts}, {
    load: function (url) {
        vm.runInThisContext(fs.readFileSync(path.join(folder, url), "utf8"));
        return Promise.resolve();
    }
}).then(function (engine) {
    console.log(JSON.stringify(texts.map(function (text) {
        var vector = engineModule.EMBED.quantize(engine.document(text));
        return {text: text, vec: Buffer.from(vector.buffer, vector.byteOffset, vector.length)
            .toString("base64")};
    })));
}, function (error) {
    console.error(String(error && error.stack || error));
    process.exit(1);
});
