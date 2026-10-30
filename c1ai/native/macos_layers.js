// Qualified Session bridge: Apple Events read/write; SQLite identity reads ONLY.
// No concurrent manual layer creation/removal/reordering is supported.
ObjC.import("AppKit");

function run(argv) {
    var dispatched = false, nativeMaskAck = null;
    try {
        var q = JSON.parse(argv[0]), app = Application(q.app_path);
        var processes = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(q.bundle_id).js;
        if (processes.length !== 1 || processes[0].bundleURL.path.js !== q.app_path || app.version() !== "16.8.6.25")
            throw new Error("version_unqualified: Layer identity requires the qualified single Mac instance");
        var process = processes[0], doc = app.currentDocument();
        var fields = {exposure: "exposure", contrast: "contrast", brightness: "brightness", saturation: "saturation",
            temperature: "temperature", tint: "tint", highlight_recovery: "highlightAdjustment",
            shadow_recovery: "shadowRecovery", white_recovery: "whiteRecovery", black_recovery: "blackRecovery"};
        var identityFields = ["exposure", "contrast", "brightness", "saturation"];
        var lumaFields = {shadow: "rangeLow", highlight: "rangeHigh", shadow_falloff: "falloffLow",
            highlight_falloff: "falloffHigh", radius: "radius", sensitivity: "sensitivity", inverted: "invert"};
        function eq(a, b) {
            if (a === b) return true;
            if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
            var ak = Object.keys(a).sort(), bk = Object.keys(b).sort();
            return JSON.stringify(ak) === JSON.stringify(bk) && ak.every(function (k) { return eq(a[k], b[k]); });
        }
        function quote(s) { return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'; }
        function appleScript(source) {
            var error = Ref(), result = $.NSAppleScript.alloc.initWithSource(source).executeAndReturnError(error);
            if (!result) throw new Error("native_call_failed: " + JSON.stringify(ObjC.deepUnwrap(error[0])));
            return result;
        }
        function photo() {
            if (process.terminated || doc.kind() !== "session" || doc.id() !== app.currentDocument.id())
                throw new Error("session_changed: Use the same open Session");
            var v = app.primaryVariant(), selected = doc.variants.whose({selected: true})();
            if (selected.length !== 1 || selected[0].id() !== v.id())
                throw new Error("single_selection_required: Select one variant");
            return {instance: {platform: "macos", pid: Number(process.processIdentifier),
                        started_at: String(process.launchDate.timeIntervalSince1970), version: app.version()},
                document: {id: doc.id(), path: String(doc.folder()) + "/" + doc.name()},
                image: {id: v.parentImage.id(), path: v.parentImage.path()},
                variant: {id: v.id(), label: v.name()}};
        }
        var bound = photo(), v = doc.variants.byId(bound.variant.id);
        if (!/^\d+$/.test(bound.variant.id) || !/^\d+$/.test(bound.image.id))
            throw new Error("invalid_native_id: Expected native numeric script IDs");
        function checkPhoto() {
            if (!eq(bound, photo())) throw new Error("target_changed: Photo changed during layer operation");
            if (q.expected_engine !== undefined && q.expected_engine !== null && v.engine() !== q.expected_engine)
                throw new Error("engine_changed: Processing engine changed");
        }
        function selectedIndex() {
            var result = appleScript('tell application ' + quote(q.app_path) + '\nget current layer of variant id ' +
                quote(bound.variant.id) + ' of document id ' + quote(bound.document.id) + '\nend tell');
            if (result.descriptorType !== 0x6f626a20 || result.descriptorForKeyword(0x77616e74).typeCodeValue !== 0x636c7972)
                throw new Error("layer_reference_changed: Expected a native layer descriptor");
            var parent = result.descriptorForKeyword(0x66726f6d);
            if (parent.descriptorForKeyword(0x77616e74).typeCodeValue !== 0x63766172 ||
                parent.descriptorForKeyword(0x666f726d).enumCodeValue !== 0x49442020 ||
                parent.descriptorForKeyword(0x73656c64).stringValue.js !== bound.variant.id)
                throw new Error("layer_reference_changed: Layer descriptor belongs to another variant");
            var form = result.descriptorForKeyword(0x666f726d).enumCodeValue;
            if (form === 0x696e6478) return result.descriptorForKeyword(0x73656c64).int32Value - 1;
            if (form === 0x6e616d65 && v.currentLayer.kind() === "background" &&
                    result.descriptorForKeyword(0x73656c64).stringValue.js === v.layers[0].name()) return 0;
            throw new Error("layer_reference_changed: Unsupported layer selector form");
        }
        function live() {
            checkPhoto();
            var count = v.layers().length, rows = [];
            for (var i = 0; i < count; i++) {
                var layer = v.layers[i], values = {};
                Object.keys(fields).forEach(function (f) {
                    var value = layer.adjustments[fields[f]]();
                    if (value !== null && (typeof value !== "number" || !isFinite(value)))
                        throw new Error("invalid_native_value: Invalid local parameter");
                    values[f] = value;
                });
                var luma = null;
                if (i > 0) {
                    luma = {};
                    Object.keys(lumaFields).forEach(function (f) {
                        var value = layer.lumaRange[lumaFields[f]]();
                        if (f === "inverted" ? typeof value !== "boolean" : typeof value !== "number" || !isFinite(value))
                            throw new Error("invalid_native_value: Invalid native luma range");
                        luma[f] = value;
                    });
                }
                rows.push({name: layer.name(), kind: layer.kind(), enabled: layer.enabled(), opacity: layer.opacity(),
                    values: values, luma_range: luma});
            }
            var selected = selectedIndex();
            if (selected < 0 || selected >= count || rows[0].kind !== "background")
                throw new Error("layer_reference_changed: Native layer index is invalid");
            checkPhoto();
            return {rows: rows, selected: selected};
        }
        function readDatabase(sql) {
            var task = $.NSTask.alloc.init, out = $.NSPipe.pipe, err = $.NSPipe.pipe;
            task.launchPath = "/usr/bin/sqlite3";
            task.arguments = ["-readonly", "-json", "-cmd", ".timeout 1000", bound.document.path, sql];
            task.standardOutput = out; task.standardError = err;
            task.launch;
            var data = out.fileHandleForReading.readDataToEndOfFile;
            task.waitUntilExit;
            if (task.terminationStatus !== 0) throw new Error("identity_store_unavailable: Cannot read qualified Session identity schema");
            return JSON.parse($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js);
        }
        function database() {
            // One read transaction, including the normal WAL. Never immutable=1.
            var sql = "BEGIN; SELECT l.Z_PK AS row_id,l.ZINDEX AS sort_index,l.ZMASKUUID AS mask_uuid," +
                "l.ZNAME AS name,l.ZOPACITY AS opacity,l.ZENABLED AS enabled,l.ZEXPOSURE AS exposure," +
                "l.ZCONTRAST AS contrast,l.ZBRIGHTNESS AS brightness,l.ZSATURATION AS saturation," +
                "v.ZVARIANTUUID AS variant_uuid,v.ZIMAGE AS image_id,v.ZPROCESSINGENGINE AS engine," +
                "v.ZADJUSTMENTLAYER AS background_id FROM ZVARIANT v JOIN ZVARIANTLAYER l ON l.ZVARIANT=v.Z_PK " +
                "WHERE v.Z_PK=" + bound.variant.id + " AND (l.ZINDEX IS NOT NULL OR l.Z_PK=v.ZADJUSTMENTLAYER) " +
                "ORDER BY l.ZINDEX; COMMIT;";
            return readDatabase(sql);
        }
        function snapshot() {
            var before = live();
            // Let Capture One flush its own state. This never writes DB/sidecar files directly.
            appleScript('tell application ' + quote(q.app_path) + '\nsave document id ' + quote(bound.document.id) + '\nend tell');
            var db = database(), after = live();
            if (!eq(before, after) || db.length !== after.rows.length)
                throw new Error("layer_identity_unsynchronized: Live and saved layer inventories disagree");
            var refs = [], used = {}, variantUUID = db[0].variant_uuid;
            var uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
            if (!uuid.test(variantUUID)) throw new Error("invalid_native_id: Missing native variant UUID");
            db.forEach(function (row, i) {
                var l = after.rows[i];
                if (row.image_id !== Number(bound.image.id) || row.variant_uuid !== variantUUID || row.engine !== v.engine() ||
                    (i === 0 ? row.row_id !== row.background_id || row.sort_index !== null :
                        row.sort_index === null || !uuid.test(row.mask_uuid) || row.name !== l.name ||
                        Boolean(row.enabled) !== l.enabled || row.opacity !== l.opacity))
                    throw new Error("layer_identity_unsynchronized: Saved layer identity does not match live order and properties");
                identityFields.forEach(function (f) {
                    if (row[f] === null || l.values[f] === null ? row[f] !== l.values[f] : Math.abs(row[f] - l.values[f]) > 0.00001)
                        throw new Error("layer_identity_unsynchronized: Saved parameters differ from live values");
                });
                var id = i === 0 ? "Background" : row.mask_uuid.toUpperCase();
                if (used[id] || !(row.row_id > 0)) throw new Error("ambiguous_layer_identity: Duplicate native layer identity");
                used[id] = true;
                // C1 rebuilds ZVARIANTLAYER rows on some mask edits. Row IDs are
                // only an alignment check; retain the real MaskUUID losslessly.
                var identity = i === 0 ? [0] : [1].concat(id.replace(/-/g, "").match(/.{8}/g).map(function (part) { return parseInt(part, 16); }));
                refs.push({runtime_id: [bound.instance.pid, Number(bound.variant.id)].concat(identity), name: l.name,
                    is_image_layer: i === 0, native_id: id,
                    variant_uuid: variantUUID, identity_source: "session_mask_uuid_readonly"});
            });
            return {layers: refs, selected_layer: refs[after.selected], details: after.rows};
        }
        function target(state) { return Object.assign({}, bound, {layer: state.selected_layer.is_image_layer ? "Background" : state.selected_layer}); }
        function values(state) {
            var index = state.layers.findIndex(function (r) { return eq(r, state.selected_layer); }), result = {};
            (q.fields || []).forEach(function (f) {
                if (!fields[f]) throw new Error("unsupported: Unsupported local numeric field");
                result[f] = state.details[index].values[f];
            });
            return result;
        }
        function savedColor(observation, saved, wantedRaw) {
            var t = observation.target, context = observation.native_context, refs = context.layer_state.layers;
            var ref = context.layer_state.selected_layer, index = refs.findIndex(function (r) { return eq(r, ref); });
            if (!saved || !saved.available || typeof saved.raw !== "string" || !saved.raw.length || index < 0 ||
                    !eq(t.instance, bound.instance) || !eq(t.document, bound.document) ||
                    !/^\d+$/.test(t.variant.id) || !/^\d+$/.test(t.image.id) ||
                    context.processing_engine !== q.expected_engine ||
                    saved.source.session_path !== bound.document.path || saved.source.variant_id !== Number(t.variant.id) ||
                    saved.source.image_id !== Number(t.image.id) || saved.source.processing_engine !== q.expected_engine ||
                    saved.source.layer_uuid !== ref.native_id || saved.source.variant_uuid !== ref.variant_uuid.toUpperCase())
                throw new Error("color_editor_snapshot_required: Explicit source identity is missing or changed");
            var rows = readDatabase("BEGIN; SELECT l.Z_PK AS row_id,l.ZINDEX AS sort_index,l.ZMASKUUID AS mask_uuid," +
                "l.ZCOLORCORRECTIONS AS raw,l.ZNAME AS name,v.ZVARIANTUUID AS variant_uuid,v.ZIMAGE AS image_id," +
                "i.ZIMAGEUUID AS image_uuid,v.ZPROCESSINGENGINE AS engine,v.ZADJUSTMENTLAYER AS background_id " +
                "FROM ZVARIANT v JOIN ZVARIANTLAYER l ON l.ZVARIANT=v.Z_PK JOIN ZIMAGE i ON i.Z_PK=v.ZIMAGE " +
                "WHERE v.Z_PK=" + t.variant.id + " AND (l.ZINDEX IS NOT NULL OR l.Z_PK=v.ZADJUSTMENTLAYER) ORDER BY l.ZINDEX; COMMIT;");
            var sourceVariant = doc.variants.byId(t.variant.id);
            if (rows.length !== refs.length || sourceVariant.layers().length !== refs.length ||
                    sourceVariant.parentImage.id() !== t.image.id || sourceVariant.parentImage.path() !== t.image.path ||
                    sourceVariant.name() !== t.variant.label || sourceVariant.engine() !== q.expected_engine)
                throw new Error("stale_source: Color Editor source photo or layer inventory changed");
            rows.forEach(function (row, i) {
                if (row.variant_uuid.toUpperCase() !== saved.source.variant_uuid || row.image_id !== saved.source.image_id ||
                        row.image_uuid.toUpperCase() !== saved.source.image_uuid || row.engine !== saved.source.processing_engine ||
                        sourceVariant.layers[i].name() !== refs[i].name ||
                        (i === 0 ? row.row_id !== row.background_id || row.sort_index !== null || !refs[i].is_image_layer :
                            row.sort_index === null || row.mask_uuid.toUpperCase() !== refs[i].native_id || row.name !== refs[i].name))
                    throw new Error("stale_source: Saved Color Editor parent or MaskUUID order changed");
            });
            if (rows[index].raw !== wantedRaw)
                throw new Error("stale_source: Complete saved Color Editor differs from the observed value");
            if (["background", "adjustment", "filled", "subject mask", "background mask"].indexOf(sourceVariant.layers[index].kind()) < 0)
                throw new Error("unsupported: Color Editor paste requires an adjustment layer");
            return {variant: sourceVariant, index: index};
        }
        function requireExplicitBasicSource(raw) {
            var lines = raw.split(";");
            if (lines[lines.length - 1] === "") lines.pop();
            var basic = [], sectors = {};
            lines.forEach(function (line) {
                var fields = line.split(","), row = fields.map(Number);
                if (row.length !== 18 || fields.some(function (part) { return !part.trim(); }) ||
                        row.some(function (value) { return !isFinite(value); }) ||
                        [0, 1].indexOf(row[0]) < 0 || [0, 1].indexOf(row[1]) < 0 ||
                        [0, 1, 2].indexOf(row[2]) < 0 || [0, 1].indexOf(row[15]) < 0)
                    throw new Error("color_editor_implicit_source: Unqualified saved Color Editor source");
                if (row[2] === 1) { basic.push(row); sectors[JSON.stringify(row.slice(6, 9))] = true; }
            });
            if (basic.length !== 9 || Object.keys(sectors).length !== 9)
                throw new Error("color_editor_implicit_source: Whole paste requires nine explicit distinct Basic sectors");
        }
        var state = snapshot(), initial = state, initialTarget = target(state), moduleState = null;
        if (q.target && (!eq(q.target, initialTarget) || !eq(q.before || {}, values(state)) ||
                (q.before_layers && !eq(q.before_layers, state))))
            throw new Error("stale_observation: Layer identity or observed state changed");
        if (q.command === "layers_adjust" || q.command === "local_set" || q.command === "local_module_set" || q.command === "masks_adjust" || q.command === "color_editor_paste") {
            if (!q.before_layers || !eq(state, q.before_layers))
                throw new Error("stale_observation: Complete layer observation required");
            if (!eq(state, snapshot())) throw new Error("layers_changed: Layer stack changed before dispatch");
            var selected = state.layers.findIndex(function (r) { return eq(r, state.selected_layer); });
            if (q.command === "color_editor_paste") {
                var sourceObservation = q.source_observation, sourceSaved = sourceObservation.native_context.color_editor_snapshot;
                requireExplicitBasicSource(sourceSaved.raw);
                var destinationObservation = {target: initialTarget, native_context: {processing_engine: v.engine(), layer_state: state}};
                var sourceColor = savedColor(sourceObservation, sourceSaved, sourceSaved.raw);
                savedColor(destinationObservation, q.destination_snapshot, q.destination_snapshot.raw);
                function readSourceColor() {
                    return createNativeAdjustments({app: app, appPath: q.app_path, bundleId: q.bundle_id,
                        adjustmentsRef: function () { return sourceColor.variant.layers[sourceColor.index].adjustments; }}).read("color_mixer");
                }
                if (!eq(localAdjustments(selected).read("color_mixer"), q.expected_state) ||
                        !eq(readSourceColor(), sourceObservation.state))
                    throw new Error("stale_observation: Native source or destination Color Editor changed");
                if (!eq(state, snapshot())) throw new Error("layers_changed: Color Editor destination layer changed");
                sourceColor = savedColor(sourceObservation, sourceSaved, sourceSaved.raw);
                savedColor(destinationObservation, q.destination_snapshot, q.destination_snapshot.raw);
                var sameLayer = sourceObservation.target.variant.id === bound.variant.id && sourceColor.index === selected;
                if (!sameLayer) {
                    dispatched = true;
                    appleScript('tell application ' + quote(q.app_path) + '\nset color editor settings of adjustments of layer ' +
                        (selected + 1) + ' of variant id ' + quote(bound.variant.id) + ' of document id ' + quote(bound.document.id) +
                        ' to color editor settings of adjustments of layer ' + (sourceColor.index + 1) + ' of variant id ' +
                        quote(sourceObservation.target.variant.id) + ' of document id ' + quote(bound.document.id) + '\nend tell');
                }
                state = snapshot();
                if (!eq(state, initial)) throw new Error("layers_changed: Color Editor paste changed unrelated layer parameters");
                savedColor(sourceObservation, sourceSaved, sourceSaved.raw);
                savedColor(destinationObservation, q.destination_snapshot, sourceSaved.raw);
                moduleState = localAdjustments(selected).read("color_mixer");
                if (!eq(moduleState, sourceObservation.state) || !eq(readSourceColor(), sourceObservation.state))
                    throw new Error("paste_readback_mismatch: Native Color Editor paste readback differs");
            } else if ((q.command === "masks_adjust" && ["create_subject", "create_background"].indexOf(q.patch.action) >= 0) ||
                    (q.command === "layers_adjust" && q.patch.action === "create_empty")) {
                var kind = {create_subject: "subject mask", create_background: "background mask", create_empty: "adjustment"}[q.patch.action];
                dispatched = true;
                appleScript('tell application ' + quote(q.app_path) + '\nset v to variant id ' + quote(bound.variant.id) +
                    ' of document id ' + quote(bound.document.id) + '\nset newLayer to make new layer at end of layers of v with properties {kind:' + kind +
                    '}\nset current layer of v to newLayer\nend tell');
                state = snapshot();
                var deadline = Date.now() + 5000;
                while (state.layers.length === initial.layers.length && Date.now() < deadline) { delay(0.25); state = snapshot(); }
                if (state.layers.length !== initial.layers.length + 1 || state.selected_layer.is_image_layer ||
                    state.layers.slice(0, -1).some(function (r, i) { return !eq(r, initial.layers[i]) || !eq(state.details[i], initial.details[i]); }) ||
                    !eq(state.selected_layer, state.layers[state.layers.length - 1]) || state.details[state.details.length - 1].kind !== kind)
                    throw new Error("layer_transition_mismatch: Native mask creation changed an unexpected layer");
            } else if (q.command === "masks_adjust" && q.patch.action === "clear_luma_range") {
                if (selected === 0) throw new Error("adjustment_layer_required: Select a local adjustment layer");
                dispatched = true;
                appleScript('tell application ' + quote(q.app_path) + '\nclear luma range layer ' + (selected + 1) +
                    ' of variant id ' + quote(bound.variant.id) + ' of document id ' + quote(bound.document.id) + '\nend tell');
                state = snapshot();
                var unchangedLuma = JSON.parse(JSON.stringify(state));
                if (unchangedLuma.details.length !== initial.details.length)
                    throw new Error("layer_transition_mismatch: Clear Luma changed the layer count");
                unchangedLuma.details[selected].luma_range = initial.details[selected].luma_range;
                if (!eq(unchangedLuma, initial))
                    throw new Error("layer_transition_mismatch: Clear Luma changed unrelated layer properties");
            } else if (q.command === "masks_adjust" && ["clear", "fill", "invert", "copy", "feather", "refine"].indexOf(q.patch.action) >= 0) {
                if (selected === 0) throw new Error("adjustment_layer_required: Select a local adjustment layer");
                var direct = 'layer ' + (selected + 1) + ' of variant id ' + quote(bound.variant.id) + ' of document id ' + quote(bound.document.id);
                var command = q.patch.action + ' mask ' + direct;
                if (q.patch.action === "copy") {
                    var source = state.layers.findIndex(function (r) { return eq(r, q.patch.source_layer); });
                    if (source <= 0 || source === selected) throw new Error("invalid_mask_source: Select a different local source layer");
                    command = 'copy mask layer ' + (source + 1) + ' of variant id ' + quote(bound.variant.id) +
                        ' of document id ' + quote(bound.document.id) + ' to layer ' + direct;
                }
                if (q.patch.action === "feather" || q.patch.action === "refine") {
                    var limit = q.patch.action === "feather" ? 100 : 300;
                    if (typeof q.patch.radius !== "number" || !isFinite(q.patch.radius) || q.patch.radius < 0 ||
                            q.patch.radius > limit || (q.patch.action === "refine" && q.patch.radius === 0))
                        throw new Error("out_of_range: Unsupported native mask filter amount");
                    command += ' amount ' + q.patch.radius;
                }
                if (q.patch.action === "fill" && q.patch.density !== 1) throw new Error("unsupported: Native fill only supports full density");
                dispatched = true;
                appleScript('tell application ' + quote(q.app_path) + '\n' + command + '\nend tell');
                if (q.patch.action === "feather" || q.patch.action === "refine")
                    nativeMaskAck = {action: q.patch.action, amount: q.patch.radius};
                state = snapshot();
                if (state.layers.length !== initial.layers.length || !eq(state.selected_layer, state.layers[selected]) ||
                        state.layers.some(function (r, i) { return i === selected ?
                            (q.patch.action !== "copy" && r.native_id !== initial.layers[i].native_id) || r.name !== initial.layers[i].name ||
                            !eq(state.details[i].values, initial.details[i].values) || state.details[i].opacity !== initial.details[i].opacity ||
                            state.details[i].enabled !== initial.details[i].enabled : !eq(r, initial.layers[i]) || !eq(state.details[i], initial.details[i]); }))
                    throw new Error("layer_transition_mismatch: Unrequested layer changed during mask operation");
            } else if (q.command === "masks_adjust") {
                if (selected === 0 || q.patch.action !== "luma_range")
                    throw new Error("unsupported: Select a local layer for luma range adjustment");
                var expectedMask = JSON.parse(JSON.stringify(state));
                Object.keys(lumaFields).forEach(function (f) { expectedMask.details[selected].luma_range[f] = q.patch[f]; });
                // Expand before narrowing: C1 couples endpoints when they cross.
                var settings = [["shadow_falloff", 0], ["highlight_falloff", 255], ["shadow", 0], ["highlight", 255]];
                Object.keys(lumaFields).forEach(function (f) { settings.push([f, q.patch[f]]); });
                settings.forEach(function (item) {
                    if (state.details[selected].luma_range[item[0]] === item[1]) return;
                    if (!eq(state, snapshot())) throw new Error("layers_changed: Layer or luma state changed before setting a parameter");
                    dispatched = true; v.layers[selected].lumaRange[lumaFields[item[0]]] = item[1];
                    var next = snapshot(), unchanged = JSON.parse(JSON.stringify(next));
                    unchanged.details[selected].luma_range = state.details[selected].luma_range;
                    if (!eq(unchanged, state)) throw new Error("layers_changed: Unrequested layer property changed during luma adjustment");
                    state = next;
                });
                Object.keys(lumaFields).forEach(function (f) {
                    var actual = state.details[selected].luma_range[f], wanted = q.patch[f];
                    if (typeof wanted === "boolean" ? actual !== wanted : Math.abs(actual - wanted) > 0.0001)
                        throw new Error("native_readback_mismatch: Luma range differs from requested value");
                    expectedMask.details[selected].luma_range[f] = actual;
                });
                if (!eq(state, expectedMask)) throw new Error("layers_changed: Unrequested change after luma adjustment");
            } else if (q.command === "layers_adjust") {
                var patch = q.patch, index = state.layers.findIndex(function (r) { return eq(r, patch.layer_ref); });
                if (index < 0) throw new Error("stale_layer_ref: Layer no longer exists");
                if (patch.action === "select") { dispatched = true; v.currentLayer = v.layers[index]; }
                else if (patch.action === "rename" && index > 0 && selected === index) {
                    dispatched = true; v.layers[index].name = patch.name;
                } else if (index > 0 && selected === index && patch.action === "set_enabled" && typeof patch.enabled === "boolean") {
                    dispatched = true; v.layers[index].enabled = patch.enabled;
                } else if (index > 0 && selected === index && patch.action === "set_opacity" &&
                        typeof patch.opacity === "number" && patch.opacity % 1 === 0 && patch.opacity >= 1 && patch.opacity <= 100) {
                    dispatched = true; v.layers[index].opacity = patch.opacity;
                } else if (index > 0 && selected === index && patch.action === "remove_created") {
                    dispatched = true; app.delete(v.layers[index]);
                    state = snapshot();
                    // runtime_id encodes pid, variant and the complete MaskUUID;
                    // it contains no row ID or layer index. These exact remaining
                    // refs therefore keep both UUID identity and relative order.
                    var kept = initial.layers.filter(function (_, i) { return i !== index; });
                    var keptDetails = initial.details.filter(function (_, i) { return i !== index; });
                    if (!eq(state.layers, kept) || !eq(state.details, keptDetails) ||
                            !state.layers.some(function (r) { return eq(r, state.selected_layer); }))
                        throw new Error("layer_transition_mismatch: Native deletion changed another layer");
                } else throw new Error("unsupported: Unsupported qualified layer action");
            } else if (q.command === "local_module_set") {
                if (selected === 0) throw new Error("adjustment_layer_required: Select a local adjustment layer");
                moduleState = localAdjustments(selected).set(q.module, q.patch, q.expected_state);
            } else {
                if (selected === 0 || ["clone", "heal"].indexOf(state.details[selected].kind) >= 0)
                    throw new Error("adjustment_layer_required: Select a local adjustment layer");
                if ((q.patch.temperature !== undefined || q.patch.tint !== undefined) &&
                        (typeof state.details[selected].values.temperature !== "number" || typeof state.details[selected].values.tint !== "number"))
                    throw new Error("unsupported: Local white balance requires both native components");
                Object.keys(q.patch).forEach(function (f) {
                    if (!fields[f]) throw new Error("unsupported: Local field is not qualified");
                    if (!eq(state, snapshot())) throw new Error("layers_changed: Local layer changed before setting a parameter");
                    dispatched = true; v.layers[selected].adjustments[fields[f]] = q.patch[f];
                    var expected = JSON.parse(JSON.stringify(state));
                    expected.details[selected].values[f] = q.patch[f];
                    state = snapshot();
                    // The entire unchanged stack remains part of each write's guard.
                    var actual = state.details[selected].values[f];
                    if (typeof actual !== "number" || Math.abs(actual - q.patch[f]) > (f === "temperature" ? 0.1 : 0.0001))
                        throw new Error("native_readback_mismatch: Local value differs from requested value");
                    expected.details[selected].values[f] = actual;
                    if (f === "temperature" || f === "tint") {
                        // C1 recalculates the WB pair with small floating-point
                        // drift. Accept only the established field tolerance.
                        var paired = f === "temperature" ? "tint" : "temperature";
                        var oldPair = expected.details[selected].values[paired], newPair = state.details[selected].values[paired];
                        if (typeof oldPair !== "number" || typeof newPair !== "number" ||
                                Math.abs(newPair - oldPair) > (paired === "temperature" ? 0.1 : 0.0001))
                            throw new Error("layers_changed: Unrequested white balance component changed");
                        expected.details[selected].values[paired] = newPair;
                    }
                    if (!eq(expected, state)) throw new Error("layers_changed: An unrelated layer property changed");
                });
            }
            if (q.command === "layers_adjust" && q.patch.action !== "create_empty") {
                var wanted = JSON.parse(JSON.stringify(state));
                if (q.patch.action === "select") wanted.selected_layer = wanted.layers[index];
                else if (q.patch.action === "rename") {
                    wanted.layers[index].name = q.patch.name; wanted.selected_layer = wanted.layers[index];
                    wanted.details[index].name = q.patch.name;
                } else if (q.patch.action === "set_enabled") wanted.details[index].enabled = q.patch.enabled;
                else if (q.patch.action === "set_opacity") wanted.details[index].opacity = q.patch.opacity;
                state = snapshot();
                if (!eq(wanted, state)) throw new Error("layer_transition_mismatch: Unrequested layer change");
            }
        } else if (["layers_read", "local_read", "local_module_read", "masks_read"].indexOf(q.command) < 0) throw new Error("unsupported: Unknown layer command");
        function localAdjustments(index) {
            return createNativeAdjustments({app: app, appPath: q.app_path, bundleId: q.bundle_id,
                adjustmentsRef: function () { return v.layers[index].adjustments; },
                adjustmentsSpecifier: function () { return 'adjustments of layer ' + (index + 1) +
                    ' of variant id ' + quote(bound.variant.id) + ' of document id ' + quote(bound.document.id); },
                guard: function () {
                    if (!eq(state, snapshot())) throw new Error("layers_changed: Local adjustment target or unrelated parameters changed");
                }, onDispatch: function () { dispatched = true; }, onStage: function () {}});
        }
        if (q.command === "local_module_read") {
            var moduleIndex = state.layers.findIndex(function (r) { return eq(r, state.selected_layer); });
            if (moduleIndex <= 0) throw new Error("adjustment_layer_required: Select a local adjustment layer");
            moduleState = localAdjustments(moduleIndex).read(q.module);
        }
        if (!eq(state, snapshot())) throw new Error("layers_changed: Layer state changed before final readback");
        return JSON.stringify({ok: true, observation: {target: target(state), values: values(state),
                native_context: {processing_engine: v.engine(), layer_state: state}},
            result: {state: moduleState || state, before_state: initial, before_target: initialTarget,
                native_mask_command_succeeded: nativeMaskAck}});
    } catch (error) {
        var message = String(error), match = message.match(/^(?:Error: )?([a-z_]+):\s*([\s\S]*)/);
        return JSON.stringify({ok: false, error: {code: match ? match[1] : "native_layer_error",
            message: match ? match[2] : message, dispatched: dispatched}});
    }
}
