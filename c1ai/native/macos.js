// Capture One's official JXA interface. JSON argv is never executable source.
ObjC.import("AppKit");

function run(argv) {
    var dispatched = false;
    var extra = {};
    try {
        var request = JSON.parse(argv[0]);
        // Navigation binds the photo, not a writable local-layer identity.
        var photoOnly = ["inventory", "select", "next", "previous", "layer_inventory"].indexOf(request.command) >= 0;
        var running = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(request.bundle_id).js;
        if (running.length !== 1 || running[0].bundleURL.path.js !== request.app_path) {
            throw new Error("application_instance_required: Run exactly one installed Capture One instance");
        }
        var process = running[0];
        var app = Application(request.app_path);
        var instance = {platform: "macos", pid: Number(process.processIdentifier),
                        started_at: String(process.launchDate.timeIntervalSince1970),
                        version: app.version()};
        var names = {exposure: "exposure", contrast: "contrast", brightness: "brightness", saturation: "saturation",
                     temperature: "temperature", tint: "tint", highlight_recovery: "highlightAdjustment",
                     shadow_recovery: "shadowRecovery", white_recovery: "whiteRecovery", black_recovery: "blackRecovery"};
        function equal(a, b) {
            if (a === b) return true;
            if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
            var keys = Object.keys(a).sort();
            return equalKeys(keys, Object.keys(b).sort()) && keys.every(function (k) { return equal(a[k], b[k]); });
        }
        function equalKeys(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

        function identity() {
            if (app.documents.length === 0) {
                throw new Error("session_required: Open the intended Session and select one photo");
            }
            var doc = app.currentDocument();
            if (doc.kind() !== "session") {
                throw new Error("session_required: Catalogs have not been qualified for this port");
            }
            var selected = doc.variants.whose({selected: true})();
            if (selected.length !== 1) {
                throw new Error("single_selection_required: Select exactly one photo variant");
            }
            var variant = app.primaryVariant();
            if (variant.id() !== selected[0].id()) {
                throw new Error("target_changed: Primary variant differs from the selected variant");
            }
            if (!photoOnly && variant.currentLayer.kind() !== "background") {
                throw new Error("background_required: The first Mac stage only supports the Image layer");
            }
            return {instance: instance,
                    document: {id: doc.id(), path: String(doc.folder()) + "/" + doc.name()},
                    image: {id: variant.parentImage.id(), path: variant.parentImage.path()},
                    variant: {id: variant.id(), label: variant.name()}, layer: photoOnly ? null : "Background"};
        }
        var before = identity();
        var doc = app.currentDocument();
        var variant = doc.variants.byId(before.variant.id);
        function valuesOf(v, fields) {
            var values = {};
            fields.forEach(function (field) {
                if (!names[field]) throw new Error("unsupported: Field is outside implemented numeric modules");
                var value = v.adjustments[names[field]]();
                if (typeof value !== "number" || !isFinite(value)) throw new Error("invalid_native_value: Invalid native value");
                values[field] = value;
            });
            return values;
        }
        function layerInventory(v) {
            var selected = {name: v.currentLayer.name(), kind: v.currentLayer.kind(), display_index: null};
            var rows = v.layers().map(function (_, index) {
                // Positional reads distinguish duplicate names; never publish
                // this display position as a reusable layer reference.
                var layer = v.layers[index];
                var row = {display_index: index, name: layer.name(), kind: layer.kind(),
                           enabled: layer.enabled(), opacity: layer.opacity(), adjustments: {},
                           luma_range: null, read_errors: {}};
                Object.keys(names).forEach(function (field) {
                    row.adjustments[field] = null;
                    try {
                        var value = layer.adjustments[names[field]]();
                        // Null is an unset/inherited local value, never an invented zero.
                        if (value !== null && (typeof value !== "number" || !isFinite(value)))
                            throw new Error("Native value unavailable");
                        row.adjustments[field] = value;
                    } catch (error) { row.read_errors[field] = String(error); }
                });
                if (row.kind !== "background") {
                    try {
                        var luma = layer.lumaRange(), values = {};
                        if (!luma) throw new Error("Native luma range unavailable");
                        var properties = {shadow: "rangeLow", highlight: "rangeHigh", shadow_falloff: "falloffLow",
                                          highlight_falloff: "falloffHigh", radius: "radius", sensitivity: "sensitivity", inverted: "invert"};
                        Object.keys(properties).forEach(function (field) {
                            var value = luma[properties[field]]();
                            if (field === "inverted" ? typeof value !== "boolean" : typeof value !== "number" || !isFinite(value))
                                throw new Error("Native luma value unavailable");
                            values[field] = value;
                        });
                        row.luma_range = values;
                    } catch (error) { row.read_errors.luma_range = String(error); }
                }
                return row;
            });
            var matches = rows.filter(function (row) { return row.name === selected.name && row.kind === selected.kind; });
            if (matches.length === 1) selected.display_index = matches[0].display_index;
            return {layers: rows, selected: selected, source: "c1_applescript", scope: "read_only_inventory",
                    stable_layer_identity: false, mask_pixels_readable: false, writable: false};
        }
        function collectionState() {
            var c = doc.currentCollection();
            if (c.synchronizing()) throw new Error("collection_busy: Collection synchronization is in progress");
            return {id: c.id(), name: c.name(), sorting_order: c.sortingOrder(), sorting_reversed: c.sortingReversed(),
                    filters: doc.filters(), ids: c.variants.id(), visible: c.variants.visible()};
        }
        function guard(target, values) {
            if (!equal(identity(), target) || process.terminated) throw new Error("target_changed: Native target changed");
            if (request.expected_engine !== undefined && request.expected_engine !== null && variant.engine() !== request.expected_engine)
                throw new Error("engine_changed: Native processing engine changed since observation");
            if (!equal(valuesOf(variant, Object.keys(values)), values)) throw new Error("stale_observation: Observed values changed");
        }
        function quoteAdjustmentSpecifier(value) {
            return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
        }
        function adjustmentsFor(v) {
            return createNativeAdjustments({
                app: app, appPath: request.app_path, bundleId: request.bundle_id,
                adjustmentsRef: function () { return v.adjustments; },
                adjustmentsSpecifier: function () {
                    return 'adjustments of variant id ' + quoteAdjustmentSpecifier(v.id()) +
                        ' of document id ' + quoteAdjustmentSpecifier(before.document.id);
                },
                guard: function () { guard(before, {}); },
                onDispatch: function () { dispatched = true; },
                onStage: function (stage) { extra.native_stage = stage; }
            });
        }
        function moduleState(v, module) { return adjustmentsFor(v).read(module); }
        function setModule(module, patch) {
            return adjustmentsFor(variant).set(module, patch, request.expected_state);
        }
        function snapshot(v) {
            // The API has no full adjustment revision counter or Skin Tone/mask values.
            // This signature covers exposed state only; the response states that limit.
            var state = {adjustments: v.adjustments.properties(), crop: v.crop(), lens: v.lensCorrection.properties(),
                         layers: [], curves: {}, basic: [], advanced: []};
            v.layers().forEach(function (layer) {
                state.layers.push({name: layer.name(), kind: layer.kind(), enabled: layer.enabled(), opacity: layer.opacity(),
                                   adjustments: layer.adjustments.properties()});
            });
            state.curves = moduleState(v, "curves").channels;
            var mixer = moduleState(v, "color_mixer");
            state.basic = mixer.colors;
            state.advanced = mixer.advanced.ranges;
            // properties() also carries live JXA object references. Explicitly
            // collected curves/colors above are the serializable native values.
            return JSON.parse(JSON.stringify(state));
        }
        if (request.target) guard(request.target, request.before || {});
        var command = request.command || "read";
        if (command === "clone") {
            var ids = variant.parentImage.variants.id();
            var sourceState = snapshot(variant);
            guard(before, request.before || {});
            dispatched = true;
            var clone = app.cloneVariant(variant, {additiveSelect: false});
            var afterIds = variant.parentImage.variants.id();
            var added = afterIds.filter(function (id) { return ids.indexOf(id) < 0; });
            if (afterIds.length !== ids.length + 1 || added.length !== 1 || clone.id() !== added[0] ||
                    !ids.every(function (id) { return afterIds.indexOf(id) >= 0; })) {
                throw new Error("clone_unverified: Candidate inventory is not one unique native clone; do not retry");
            }
            extra = {source_target: before, before_ids: ids, after_ids: afterIds,
                     clone_id: clone.id(), copied_state: sourceState};
            // additiveSelect=false does not select the clone on this build.
            guard(before, request.before || {});
            if (!equal(snapshot(variant), sourceState) || !equal(snapshot(clone), sourceState)) {
                throw new Error("clone_unverified: Copied settings differ; do not retry");
            }
            app.deselect(doc, {variant: variant});
            app.select(doc, {variant: clone});
            var cloneTarget = identity();
            if (!equal(cloneTarget.instance, before.instance) || !equal(cloneTarget.document, before.document) ||
                    !equal(cloneTarget.image, before.image) || cloneTarget.variant.id !== clone.id() ||
                    !equal(snapshot(variant), sourceState) || !equal(snapshot(clone), sourceState)) {
                throw new Error("clone_unverified: Candidate selection or copied settings differ; do not retry");
            }
            variant = clone;
            before = cloneTarget;
        } else if (command === "layer_inventory") {
            extra.layer_inventory = layerInventory(variant);
            if (!equal(extra.layer_inventory, layerInventory(variant)))
                throw new Error("layers_changed: Layer information changed while reading");
        } else if (command === "inventory") {
            var collection = collectionState(), rows = [], indexes = [];
            collection.ids.forEach(function (id, i) { if (request.scope === "all" || collection.visible[i]) indexes.push(i); });
            indexes.slice(request.offset || 0, (request.offset || 0) + (request.limit || 200)).forEach(function (i) {
                var item = doc.variants.byId(collection.ids[i]), path = item.parentImage.path();
                rows.push({variant_id: Number(collection.ids[i]), filename: path.split("/").pop(), path: path,
                           selected: collection.ids[i] === before.variant.id, visible: collection.visible[i],
                           collection_index: i, variant_position: item.position()});
            });
            if (!equal(collectionState(), collection)) throw new Error("collection_changed: Collection changed while listing");
            extra = {collection: collection, items: rows, total: indexes.length};
        } else if (command === "select" || command === "next" || command === "previous") {
            var collection = collectionState();
            if (request.expected_collection && !equal(collection, request.expected_collection)) {
                throw new Error("collection_changed: Read the collection again before selecting");
            }
            if (request.variant_uuid) throw new Error("unsupported: Mac script IDs are not UUIDs");
            var destinationId = null;
            if (command === "select") {
                var destination = doc.variants.byId(String(request.variant_id));
                destinationId = destination.id();
                if (collection.ids.indexOf(destinationId) < 0 || !destination.visible())
                    throw new Error("variant_not_visible: Select only in the current filtered collection");
                guard(before, request.before || {});
                if (destinationId !== before.variant.id) {
                    dispatched = true;
                    app.deselect(doc, {variant: variant});
                    app.select(doc, {variant: destination});
                }
            } else {
                guard(before, request.before || {});
                dispatched = true;
                app.changeSelection(doc, {to: command + " variant"});
            }
            var selectedTarget = identity();
            if (!equal(selectedTarget.instance, before.instance) || !equal(selectedTarget.document, before.document) ||
                    !equal(collectionState(), collection) || (destinationId !== null && selectedTarget.variant.id !== destinationId))
                throw new Error("selection_unverified: Native selection or collection did not match");
            variant = doc.variants.byId(selectedTarget.variant.id);
            before = selectedTarget;
            extra = {native_command: command === "select" ? "select" : "change selection to " + command + " variant",
                     collection_id: collection.id, collection: collection};
        } else if (command === "set") {
            var current = Object.assign({}, request.before);
            Object.keys(request.patch).forEach(function (field) {
                if (!names[field] || !Object.prototype.hasOwnProperty.call(current, field)) throw new Error("unsupported: Unobserved field");
                guard(before, current);
                dispatched = true;
                variant.adjustments[names[field]] = request.patch[field];
                // Do not adopt concurrent changes to fields we have not written.
                // The next guard must still compare their original observed values.
                current[field] = valuesOf(variant, [field])[field];
            });
        } else if (command === "snapshot") {
            extra.render_state = snapshot(variant);
        } else if (command === "module_read") {
            extra.state = moduleState(variant, request.module);
        } else if (command === "module_set") {
            extra.state = setModule(request.module, request.patch);
        } else if (command === "compare_candidate") {
            var source = doc.variants.byId(request.source_id);
            var sourceSnapshot = snapshot(source), candidateSnapshot = snapshot(variant);
            extra = {source_id: source.id(), candidate_id: variant.id(),
                     parent_ids: source.parentImage.variants.id(),
                     same_image: source.parentImage.path() === variant.parentImage.path(),
                     equal_exposed_state: equal(sourceSnapshot, candidateSnapshot),
                     source_state: sourceSnapshot, candidate_state: candidateSnapshot,
                     source_values: valuesOf(source, request.fields), candidate_values: valuesOf(variant, request.fields)};
        } else if (command !== "read") {
            throw new Error("unsupported: Unknown native operation");
        }
        var values = valuesOf(variant, request.fields);
        var after = identity();
        if (process.terminated || !equal(before, after)) {
            throw new Error("target_changed: Target changed during native read");
        }
        return JSON.stringify({ok: true, observation: {target: after, values: values,
            native_context:{processing_engine:variant.engine()}}, result: extra});
    } catch (error) {
        var message = String(error.message || error);
        var code = "apple_events_error";
        var prefix = message.match(/^([a-z_]+): /);
        if (prefix) code = prefix[1];
        if (error.errorNumber === -1743) code = "automation_permission_denied";
        if (error.errorNumber === -1712) code = "apple_events_timeout";
        return JSON.stringify({ok: false, error: {code: code, message: message, dispatched: dispatched, details: extra}});
    }
}
