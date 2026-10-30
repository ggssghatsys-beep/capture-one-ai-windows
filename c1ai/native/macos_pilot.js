// Official Apple Events only: short-lived, explicitly configured Pilot sharing.
ObjC.import("AppKit");
function run(argv) {
    try {
        var r = JSON.parse(argv[0]);
        var processes = $.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.captureone.captureone16").js;
        if (processes.length !== 1 || processes[0].bundleURL.path.js !== "/Applications/Capture One.app" ||
            Number(processes[0].processIdentifier) !== r.target.instance.pid ||
            String(processes[0].launchDate.timeIntervalSince1970) !== r.target.instance.started_at)
            throw Error("Capture One instance changed");
        var app = Application("/Applications/Capture One.app"), doc = app.documents.byId(r.target.document.id);
        if (String(doc.folder()) + "/" + doc.name() !== r.target.document.path) throw Error("Session changed");
        var s = doc.serverSettings;
        function settings() {
            return {name:s.name(), collection:s.servedCollection().id(), port:s.mobilePort(),
                mobile:s.mobileServing(), web:s.webServing(), rate:s.mobileRate(), tag:s.mobileColorTag(),
                adjust:s.mobileAdjust(), capture:s.mobileCapture()};
        }
        function same(a,b) { return JSON.stringify(a) === JSON.stringify(b); }
        function selectedToolTabID() {
            var ids=doc.toolTabs.whose({selected:true}).id();
            if (ids.length!==1 || typeof ids[0]!=="string" || !ids[0])
                throw Error("Selected tool tab identity is unavailable");
            return ids[0];
        }
        function collectionIdentity(c) {
            return {id:c.id(), name:c.name(), kind:c.kind(), folder:String(c.folder() || "")};
        }
        function sourceCollection() {
            var c=doc.currentCollection();
            if (!same(collectionIdentity(c),r.source_collection) || c.synchronizing() ||
                c.variants.id().indexOf(r.target.variant.id)<0)
                throw Error("Selected photo is outside the approved current collection");
            return c;
        }
        function ownedAlbum() {
            var found=doc.collections.whose({name:r.name})();
            if (found.length>1) throw Error("Temporary album identity is ambiguous");
            if (!found.length) return null;
            var c=found[0];
            if (c.kind()!=="album" || !c.user()) throw Error("Temporary collection is not an owned album");
            return c;
        }
        function rowsOf(c) {
            return c.variants().map(function(v) { return {id:v.id(), image_id:v.parentImage.id(),
                path:v.parentImage.path(), position:v.position()}; });
        }
        function approvedRows(rows, emptyAllowed) {
            if ((!emptyAllowed && (!rows.length || !rows.some(v=>v.id===r.target.variant.id))) ||
                rows.length>256 || rows.some(v=>r.images[v.image_id]!==v.path))
                throw Error("Collection exceeds approved photo scope");
            return rows;
        }
        function selectedTarget() {
            var v = app.primaryVariant();
            if (app.currentDocument().id() !== r.target.document.id || v.id() !== r.target.variant.id ||
                v.parentImage.id() !== r.target.image.id || v.parentImage.path() !== r.target.image.path ||
                doc.variants.whose({selected:true}).id().length !== 1) throw Error("Selected target changed");
            // Scope/old recovery receipts retain the original Background rule.
            // Qualified render receipts preserve the exact selected local layer.
            if (!r.selected_layer) {
                if (v.currentLayer.kind() !== "background") throw Error("Selected target changed");
                return;
            }
            var q=v=>'"'+String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"')+'"';
            var source='tell application "/Applications/Capture One.app"\nget current layer of variant id '+
                q(r.target.variant.id)+' of document id '+q(r.target.document.id)+'\nend tell';
            var err=Ref(), selected=$.NSAppleScript.alloc.initWithSource(source).executeAndReturnError(err);
            if (!selected || selected.descriptorType!==0x6f626a20 ||
                selected.descriptorForKeyword(0x77616e74).typeCodeValue!==0x636c7972)
                throw Error("Selected native layer reference unavailable");
            var parent=selected.descriptorForKeyword(0x66726f6d);
            if (parent.descriptorForKeyword(0x77616e74).typeCodeValue!==0x63766172 ||
                parent.descriptorForKeyword(0x666f726d).enumCodeValue!==0x49442020 ||
                parent.descriptorForKeyword(0x73656c64).stringValue.js!==r.target.variant.id)
                throw Error("Selected layer belongs to another variant");
            var form=selected.descriptorForKeyword(0x666f726d).enumCodeValue;
            var index=form===0x696e6478 ? selected.descriptorForKeyword(0x73656c64).int32Value-1 :
                form===0x6e616d65 && v.currentLayer.kind()==="background" &&
                selected.descriptorForKeyword(0x73656c64).stringValue.js===v.layers[0].name() ? 0 : -1;
            if (index!==r.selected_index || index<0) throw Error("Selected layer changed");
            var details=v.layers().map(function(_, index) {
                var layer=v.layers[index];
                var values={};
                var localFields={exposure:"exposure",contrast:"contrast",brightness:"brightness",saturation:"saturation",
                    temperature:"temperature",tint:"tint",highlight_recovery:"highlightAdjustment",
                    shadow_recovery:"shadowRecovery",white_recovery:"whiteRecovery",black_recovery:"blackRecovery"};
                Object.keys(localFields).forEach(function(field) {
                    values[field]=layer.adjustments[localFields[field]]();
                });
                var luma=null;
                if (layer.kind()!=="background") {
                    var range=layer.lumaRange();
                    luma={shadow:range.rangeLow(),highlight:range.rangeHigh(),shadow_falloff:range.falloffLow(),
                          highlight_falloff:range.falloffHigh(),radius:range.radius(),sensitivity:range.sensitivity(),
                          inverted:range.invert()};
                }
                return {name:layer.name(),kind:layer.kind(),enabled:layer.enabled(),opacity:layer.opacity(),
                        values:values,luma_range:luma};
            });
            if (!same(details,r.layer_details)) throw Error("Layer stack changed during Pilot rendering");
        }
        function inspect() {
            selectedTarget();
            var c, rows;
            if (r.temporary_album) {
                var source=sourceCollection();
                c=ownedAlbum();
                rows=c ? rowsOf(c) : rowsOf(source).filter(v=>v.image_id===r.target.image.id);
            } else { c=doc.collections.byId(r.collection_id); rows=rowsOf(c); }
            var result={settings:settings(), rows:approvedRows(rows,false)};
            // Capture the original page only before the durable startup receipt.
            // The rendered image's state guard does not depend on which tab is visible.
            if (!Object.prototype.hasOwnProperty.call(r,"before")) result.tool_tab_id=selectedToolTabID();
            return result;
        }
        var result;
        if (r.command === "scope") {
            selectedTarget();
            result={collection:collectionIdentity(doc.currentCollection()), settings:settings()};
        } else if (r.command === "inspect") result = inspect();
        else if (r.command === "start") {
            var initial = inspect();
            if (!same(initial.settings,r.before) || initial.settings.mobile || initial.settings.web)
                throw Error("Existing Pilot service/settings changed; leave it untouched");
            if (Object.prototype.hasOwnProperty.call(r,"before_tool_tab_id") &&
                selectedToolTabID()!==r.before_tool_tab_id)
                throw Error("Selected tool tab changed before Pilot startup");
            var config = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(r.config_path,4,null).js);
            var permission=r.temporary_album ? config.version===2 && config.scope==="selected_photo_in_collection" &&
                same(config.source_collection,r.source_collection) :
                config.version===1 && config.collection_id===r.collection_id && same(config.images,r.images);
            if (!same(config.document,r.target.document) || !permission ||
                typeof config.password !== "string" || config.password.length < 32)
                throw Error("Pilot permission configuration changed");
            var share;
            if (r.temporary_album) {
                if (ownedAlbum()) throw Error("Temporary album already exists; do not replay startup");
                // A C1 album contains image references. Adding a variant adds all
                // variants of that RAW; no file is moved or exported.
                var q=v=>'"'+String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"')+'"';
                var source='tell application "/Applications/Capture One.app"\ntell document id '+q(r.target.document.id)+
                    '\nmake new collection with properties {name:'+q(r.name)+', kind:album}\nend tell\nend tell';
                var err=Ref(), made=$.NSAppleScript.alloc.initWithSource(source).executeAndReturnError(err);
                if (!made) throw Error("Could not create temporary preview album");
                share=ownedAlbum();
                app.addInside(share,{variants:[doc.variants.byId(r.target.variant.id)]});
                approvedRows(rowsOf(share),false);
                sourceCollection();
            } else share=doc.collections.byId(r.collection_id);
            // Claim the inactive server before changing any other settings, so
            // finally/explicit cleanup can recognize a partial startup.
            s.name=r.name;
            s.password=config.password;
            s.mobileRate=false; s.mobileColorTag=false; s.mobileAdjust=false; s.mobileCapture=false;
            s.servedCollection=share; s.mobilePort=0;
            var protectedSettings=settings();
            if (protectedSettings.rate || protectedSettings.tag || protectedSettings.adjust || protectedSettings.capture ||
                protectedSettings.web || protectedSettings.collection!==share.id() || protectedSettings.name!==r.name)
                throw Error("Pilot permissions/scope did not apply");
            s.mobileServing=true;
            result=inspect();
            if (!result.settings.mobile || result.settings.port < 1) throw Error("Pilot did not start");
        } else if (r.command === "stop") {
            var current=settings();
            if (!same(current,r.before)) {
                if (current.name!==r.name || current.web) throw Error("Pilot ownership changed; cleanup requires inspection");
                s.mobileServing=false;
                if (s.mobileServing()) throw Error("Pilot did not stop");
                s.servedCollection=doc.collections.byId(r.before.collection); s.mobilePort=r.before.port;
                s.mobileRate=r.before.rate; s.mobileColorTag=r.before.tag;
                s.mobileAdjust=r.before.adjust; s.mobileCapture=r.before.capture;
                s.name=r.before.name;
                if (!same(settings(),r.before)) throw Error("Pilot settings did not restore");
            }
            if (r.temporary_album) {
                var album=ownedAlbum();
                if (album) {
                    approvedRows(rowsOf(album),true);
                    var browsing=doc.currentCollection(), selected=doc.variants.whose({selected:true}).id();
                    var browsingID=browsing ? browsing.id() : null;
                    if (selected.length>256) throw Error("Too many selected variants to preserve during album cleanup");
                    // Delete only our uniquely named organizational album. Never
                    // delete variants/images or use the physical move command.
                    app.delete(album);
                    if (ownedAlbum()) throw Error("Temporary album did not disappear");
                    // This C1 build clears the browser even when a different
                    // album was deleted. Restore exactly its pre-delete state.
                    var currentCollection=doc.currentCollection();
                    if (!currentCollection && !doc.variants.whose({selected:true}).id().length && browsingID) {
                        doc.currentCollection=doc.collections.byId(browsingID);
                        var restoredSelection=doc.variants.whose({selected:true})();
                        if (restoredSelection.length) app.deselect(doc,{variants:restoredSelection});
                        if (selected.length) app.select(doc,{variants:selected.map(id=>doc.variants.byId(id))});
                    }
                    if (browsingID && (!doc.currentCollection() || doc.currentCollection().id()!==browsingID ||
                        !same(doc.variants.whose({selected:true}).id(),selected)))
                        throw Error("Browser selection changed during temporary album cleanup");
                }
                if (!doc.currentCollection()) throw Error("Browser restoration needs inspection before closing the cleanup receipt");
            }
            var tabRestored=null;
            // Old recovery receipts predate page preservation and remain valid.
            if (Object.prototype.hasOwnProperty.call(r,"before_tool_tab_id")) {
                if (typeof r.before_tool_tab_id!=="string" || !r.before_tool_tab_id ||
                    app.currentDocument().id()!==r.target.document.id)
                    throw Error("Original tool tab restoration needs the bound Session");
                var tabs=doc.toolTabs.whose({id:r.before_tool_tab_id})();
                if (tabs.length!==1) throw Error("Original tool tab is missing or ambiguous; inspect the workspace");
                if (selectedToolTabID()!==r.before_tool_tab_id) tabs[0].selected=true;
                if (selectedToolTabID()!==r.before_tool_tab_id) throw Error("Original tool tab did not restore");
                tabRestored=true;
            }
            result={restored:true, temporary_album_removed:!!r.temporary_album,
                    tool_tab_restored:tabRestored, password:"authorized_password_retained"};
        } else throw Error("Unknown Pilot control command");
        return JSON.stringify({ok:true,result:result});
    } catch(e) {
        var message=String(e.message || e);
        if (typeof config!=="undefined" && typeof config.password==="string")
            message=message.split(config.password).join("[redacted]");
        return JSON.stringify({ok:false,error:message});
    }
}
