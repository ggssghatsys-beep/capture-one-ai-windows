// Shared official adjustment-settings operations for Image and local layers.
// The caller supplies a freshly resolved native reference and its identity guard.
// No persistence access, layer selection, default materialization or UI fallback.
function createNativeAdjustments(context) {
    var channels = ["rgb", "red", "green", "blue", "luma"];
    var levelNames = {input_black: "levelShadow", input_white: "levelHighlight", midtone: "levelMidtone",
                      output_black: "levelTargetShadow", output_white: "levelTargetHighlight"};
    var colorNames = {hue: "hueChange", saturation: "saturationChange", lightness: "lightnessChange",
                      hue_low: "hueStart", hue_high: "hueEnd", saturation_low: "saturationStart",
                      saturation_high: "saturationEnd", smoothness: "smoothness", enabled: "enabled"};
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function finiteNumber(value, field) {
        if (typeof value !== "number" || !isFinite(value))
            throw new Error("invalid_native_value: Native " + field + " is not a finite number");
        return value;
    }
    function quoteAS(value) { return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'; }
    function makeInContainer(container, statement) {
        var source = 'tell application ' + quoteAS(context.appPath) + '\n' +
            'tell ' + container + ' of ' + context.adjustmentsSpecifier() + '\n' + statement + '\nend tell\nend tell';
        var script = $.NSAppleScript.alloc.initWithSource(source), error = Ref();
        var made = script.executeAndReturnError(error);
        if (!made) throw new Error("native_creation_failed: " + JSON.stringify(ObjC.deepUnwrap(error[0])));
    }
    function close(a, b, tolerance) {
        tolerance = tolerance === undefined ? 0.0001 : tolerance;
        if (typeof a === "number" && typeof b === "number") return isFinite(a) && isFinite(b) && Math.abs(a - b) <= tolerance;
        if (a === b) return true;
        if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
        return JSON.stringify(Object.keys(a).sort()) === JSON.stringify(Object.keys(b).sort()) &&
            Object.keys(a).every(function (key) { return close(a[key], b[key], tolerance); });
    }
    function read(module) {
        var a = context.adjustmentsRef(), state = {channels: {}};
        if (module === "curves") {
            channels.forEach(function (channel) {
                // Read primitives immediately: JXA properties() can retain live specifiers.
                state.channels[channel] = a[channel + "Curve"].curvePoints().map(function (p) {
                    return [finiteNumber(p.brightness(), channel + " curve brightness") / 100,
                        finiteNumber(p.amount(), channel + " curve amount") / 100];
                });
            });
        } else if (module === "levels") {
            channels.slice(0, 4).forEach(function (channel) {
                var suffix = channel.charAt(0).toUpperCase() + channel.slice(1), row = {};
                Object.keys(levelNames).forEach(function (key) { row[key] = a[levelNames[key] + suffix](); });
                state.channels[channel] = row;
            });
        } else if (module === "color_mixer") {
            state = {mode: "basic", colors: {}, advanced: {ranges: []}, skin: {available: false}};
            var basic = a.colorEditorSettings.basicColorCorrections;
            // Read primitive columns in four Apple Events. The surrounding
            // target/state guards still apply; these reads are not atomic.
            var names = basic.name(), hues = basic.hueChange(),
                saturations = basic.saturationChange(), lightnesses = basic.lightnessChange();
            if (![names, hues, saturations, lightnesses].every(function (column) {
                return Array.isArray(column) && column.length === 9;
            })) throw new Error("incomplete_mixer: Expected four complete Basic sector columns");
            var sectors = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta", "all"];
            for (var i = 0; i < names.length; i++) {
                var name = names[i] === "pink" ? "magenta" : names[i];
                if (typeof name !== "string" || sectors.indexOf(name) < 0 ||
                    Object.prototype.hasOwnProperty.call(state.colors, name))
                    throw new Error("incomplete_mixer: Expected nine distinct known Basic sectors");
                state.colors[name] = {
                    hue: finiteNumber(hues[i], name + " Basic hue"),
                    saturation: finiteNumber(saturations[i], name + " Basic saturation"),
                    lightness: finiteNumber(lightnesses[i], name + " Basic lightness")};
            }
            if (Object.keys(state.colors).length !== 9) throw new Error("incomplete_mixer: Expected nine distinct Basic sectors");
            var advanced = a.colorEditorSettings.advancedColorCorrections;
            for (var index = 0; index < advanced.length; index++) {
                var correction = advanced[index];
                var row = {index: index, rgb: [correction.red(), correction.green(), correction.blue()]};
                Object.keys(colorNames).forEach(function (key) { row[key] = correction[colorNames[key]](); });
                state.advanced.ranges.push(row);
            }
        } else if (module === "color_balance") {
            var preferences = $.NSUserDefaults.alloc.initWithSuiteName(context.bundleId);
            var layout = ObjC.unwrap(preferences.objectForKey("colorWheelLayout"));
            if (layout === undefined) layout = ObjC.unwrap($.NSDictionary.dictionaryWithContentsOfFile(
                context.appPath + "/Contents/Resources/UserDefaults.plist").objectForKey("colorWheelLayout"));
            if (Number(layout) !== 0) throw new Error("wheel_layout_unqualified: Only the installed red-east wheel layout is qualified");
            state = {values: {}, wheel_layout: "red_east"};
            ["Master", "Shadow", "Midtone", "Highlight"].forEach(function (wheel) {
                var prefix = wheel.toLowerCase(), base = "colorBalance" + wheel;
                var hue = finiteNumber(a[base + "Hue"](), base + "Hue") * Math.PI / 180;
                var saturation = finiteNumber(a[base + "Saturation"](), base + "Saturation");
                state.values[prefix + "_x"] = 50 * saturation * Math.cos(hue);
                state.values[prefix + "_y"] = 50 * saturation * Math.sin(hue);
                if (wheel !== "Master") state.values[prefix + "_lightness"] =
                    50 * finiteNumber(a[base + "Lightness"](), base + "Lightness");
            });
        } else throw new Error("unsupported: Native structured module is unavailable");
        return state;
    }
    function set(module, patch, expectedState) {
        var expected = copy(expectedState), a = context.adjustmentsRef();
        function check() {
            context.guard();
            var actual = read(module);
            if (!close(actual, expected, module === "color_balance" ? 0.003 : 0.0001))
                throw new Error("stale_module: Observed module changed");
            return actual;
        }
        function write(object, property, value, update) {
            context.onStage("set " + property);
            check(); context.onDispatch(); object[property] = value; update(); check();
        }
        check();
        if (module === "levels") {
            var channel = patch.channel, suffix = channel.charAt(0).toUpperCase() + channel.slice(1);
            var order = [];
            [["input_black", "input_white"], ["output_black", "output_white"]].forEach(function (pair) {
                // Widen the old interval first when both endpoints move past it.
                if (patch[pair[0]] !== undefined && patch[pair[0]] >= expected.channels[channel][pair[1]])
                    order.push(pair[1], pair[0]);
                else order.push(pair[0], pair[1]);
            });
            order.push("midtone");
            order.forEach(function (key) {
                if (patch[key] !== undefined) write(a, levelNames[key] + suffix, patch[key], function () {
                    expected.channels[channel][key] = patch[key];
                });
            });
        } else if (module === "curves") {
            var curve = a[patch.channel + "Curve"], points = expected.channels[patch.channel];
            // C1 requires the two endpoints to remain present while replacing interior points.
            while (points.length > 2) {
                check(); context.onDispatch(); context.app.delete(curve.curvePoints[1]); points.splice(1, 1); check();
            }
            write(curve.curvePoints[0], "amount", patch.points[0][1] * 100, function () { points[0][1] = patch.points[0][1]; });
            write(curve.curvePoints[1], "amount", patch.points[patch.points.length - 1][1] * 100, function () {
                points[1][1] = patch.points[patch.points.length - 1][1];
            });
            patch.points.slice(1, -1).forEach(function (point) {
                context.onStage("create curve point");
                check(); context.onDispatch();
                // C1's curve insertion implements the AppleScript tell-container
                // form; JXA make(at: curve) is rejected by its scripting object.
                makeInContainer(patch.channel + " curve", 'make new curve point with properties {brightness:' +
                    (point[0] * 100) + ', amount:' + (point[1] * 100) + '}');
                points.splice(points.length - 1, 0, point.slice()); check();
            });
        } else if (module === "color_balance") {
            var wheel = {master: "Master", shadows: "Shadow", midtones: "Midtone", highlights: "Highlight"}[patch.wheel];
            var prefix = wheel.toLowerCase(), base = "colorBalance" + wheel;
            if (patch.x !== undefined || patch.y !== undefined) {
                var x = patch.x === undefined ? expected.values[prefix + "_x"] : patch.x;
                var y = patch.y === undefined ? expected.values[prefix + "_y"] : patch.y;
                var saturation = Math.sqrt(x * x + y * y) / 50;
                if (saturation > 1) throw new Error("out_of_range: Color wheel point must be inside radius 50");
                var oldHue = finiteNumber(a[base + "Hue"](), base + "Hue") * Math.PI / 180;
                write(a, base + "Saturation", saturation, function () {
                    expected.values[prefix + "_x"] = 50 * saturation * Math.cos(oldHue);
                    expected.values[prefix + "_y"] = 50 * saturation * Math.sin(oldHue);
                });
                if (saturation > 0) write(a, base + "Hue", (Math.atan2(y, x) * 180 / Math.PI + 360) % 360, function () {
                    expected.values[prefix + "_x"] = x; expected.values[prefix + "_y"] = y;
                });
            }
            if (patch.lightness !== undefined) write(a, base + "Lightness", patch.lightness / 50, function () {
                expected.values[prefix + "_lightness"] = patch.lightness;
            });
        } else if (module === "color_mixer" && patch.mode === "basic") {
            var color = patch.color, nativeName = color === "magenta" ? "pink" : color;
            var basic = a.colorEditorSettings.basicColorCorrections, match = [];
            for (var i = 0; i < basic.length; i++) if (basic[i].name() === nativeName) match.push(i);
            if (match.length !== 1) throw new Error("color_identity_unavailable: Basic sector must be unique");
            ["hue", "saturation", "lightness"].forEach(function (key) {
                if (patch[key] !== undefined) write(basic[match[0]], colorNames[key], patch[key], function () {
                    expected.colors[color][key] = patch[key];
                });
            });
        } else if (module === "color_mixer" && patch.mode === "advanced") {
            var action = patch.action || "update", index = patch.index;
            if (action === "add") {
                context.onStage("create Advanced range");
                check(); context.onDispatch();
                makeInContainer("color editor settings", 'make new advanced color correction with properties {red:' +
                    patch.rgb[0] + ', green:' + patch.rgb[1] + ', blue:' + patch.rgb[2] + '}');
                var created = read(module), newRows = created.advanced.ranges;
                index = expected.advanced.ranges.length;
                if (newRows.length !== index + 1 || !close(created.colors, expected.colors) ||
                        !close(newRows.slice(0, index), expected.advanced.ranges) || !close(newRows[index].rgb, patch.rgb)) {
                    throw new Error("range_creation_unverified: New range or existing sectors differ");
                }
                // Only the new object's native defaults may enter the baseline.
                expected.advanced.ranges.push(copy(newRows[index])); check();
            }
            var row = expected.advanced.ranges[index];
            if (!row) throw new Error("range_missing: Read the Advanced range before editing");
            var correction = a.colorEditorSettings.advancedColorCorrections[index];
            if (action === "remove") {
                check(); context.onDispatch(); context.app.delete(correction);
                expected.advanced.ranges.splice(index, 1);
                expected.advanced.ranges.forEach(function (r, i) { r.index = i; }); check();
            } else {
                if (patch.rgb !== undefined && action !== "add") {
                    context.onStage("set Advanced RGB origin");
                    check(); context.onDispatch();
                    correction.properties = {red:patch.rgb[0], green:patch.rgb[1], blue:patch.rgb[2]};
                    row.rgb = patch.rgb.slice(); check();
                }
                var colorOrder = Object.keys(colorNames);
                if (patch.hue_low !== undefined && row.hue_high - patch.hue_low > 359) {
                    colorOrder.splice(colorOrder.indexOf("hue_high"), 1);
                    colorOrder.unshift("hue_high");
                }
                colorOrder.forEach(function (key) {
                    if (patch[key] !== undefined) write(correction, colorNames[key], patch[key], function () { row[key] = patch[key]; });
                });
            }
        } else throw new Error("unsupported: This structured operation has no native implementation");
        return check();
    }
    return {read: read, set: set};
}
