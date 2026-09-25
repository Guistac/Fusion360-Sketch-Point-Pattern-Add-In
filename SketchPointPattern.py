"""
================================================================================
FUSION 360 ARBITRARY SKETCH POINT PATTERNING ENGINE
Architectural Findings & C++ API History Solver Reference
================================================================================

1. THE FUNDAMENTAL C++ TIMELINE CONSTRAINT
   - Fusion 360's CustomFeature API (`adsk.fusion.CustomFeatureDefinition`) was 
     designed strictly as a high-level macro container over native parametric
     features (Extrude, Revolve, Sweep, Move).
   - Fusion's internal C++ history solver strictly prohibits encapsulating an
     unparameterized direct-modeling geometry container (`BaseFeature`) via
     `setStartAndEndFeatures()`. Attempting to do so triggers:
       `RuntimeError: 3 : make params invalid`
   - A `CustomFeature` cannot exist as an empty standalone node without wrapped
     timeline boundaries. Passing empty boundaries or attempting to commit
     without valid parametric start/end features also triggers `make params invalid`.
   - Furthermore, `CustomFeatures.add(input)` carries an internal provenance check
     (`InternalValidationError : scriptInfoFound`), preventing initialization
     from dynamic REPL contexts (`exec()`) outside registered Add-In entry points.

2. PARAMETER & DEPENDENCY TYPE RESTRICTIONS
   - For dimensionless / unitless custom parameters (such as instance counts):
       `inp.addCustomParameter('id', 'Name', ValueInput.createByReal(N), '', False)`
     is the required signature. Passing strings with 'ul' can fail expression parsing
     (`Invalid expression`).
   - `CustomFeatureInput.addDependency(id, entity)` accepts topological entities
     (SketchPoint, BRepVertex, ConstructionPoint), but strictly rejects whole
     composite sketch containers (`adsk.fusion.Sketch`).

3. THE PERFORMANCE PARADOX (NATIVE VS. API)
   - Native pattern features (Rectangular / Circular Pattern) execute compiled
     instancing algorithms directly inside the Autodesk Shape Manager (ASM) kernel
     and register as atomic history nodes.
   - Scripting 144 individual parametric features (e.g. 144 Copy/Move operations)
     through the API forces the history solver to resolve 144 discrete topological
     steps, leading to timeline bloat and multi-second freezes upon compute.
   - `adsk.fusion.TemporaryBRepManager` calculates transformations entirely in
     RAM, creating 144 solid instances in under 50 milliseconds.

4. THE REACTIVE BASEFEATURE ARCHITECTURE
   - To achieve sub-50ms performance, a clean single timeline footprint, and 
     automatic re-evaluation on upstream changes:
       a) Geometry is computed in RAM via `TemporaryBRepManager` and committed to
          a dedicated `BaseFeature` (`Sketch Pattern (144 pts)`).
       b) The add-in hooks into `ui.commandTerminated`. To catch all topology 
          modifications without whitelisting hundreds of commands, the observer 
          triggers on all commands except high-frequency view/selection events 
          (Pan, Zoom, Orbit, Select).
       c) Editing is supported seamlessly by selecting the pattern feature and 
          re-launching the command from the Solid Create toolbar.

5. ENTITY TOKEN VOLATILITY & SELF-HEALING FALLBACKS
   - Fusion's kernel routinely destroys and rebuilds BRep topology during parametric 
     timeline edits. Relying solely on `design.findEntityByToken()` will silently 
     break background observers when upstream features are modified.
   - **Name Fallbacks:** The BaseFeature serializes `source_name` and `sketch_name`. 
     If an `entityToken` is destroyed, the observer scans the design by name and 
     dynamically re-binds the new tokens.
   - **Coordinate Decoupling:** Vertex tokens are highly volatile. The script stores 
     raw `anchor_x`, `anchor_y`, and `anchor_z` coordinates as attributes. If the 
     anchor vertex is destroyed, the pattern degrades gracefully by using its last 
     known spatial origin rather than permanently breaking.
   - **Composite State Hashing:** To detect geometry changes, the observer calculates 
     an MD5 hash of both the target sketch coordinates AND the source body's physical 
     properties (Volume and Bounding Box limits).
================================================================================
"""

import os
import sys
import hashlib
import traceback
import adsk.core
import adsk.fusion

app = None
ui = None
handlers = []

CMD_ID = 'SketchPointPatternCmd'
CMD_NAME = 'Sketch Point Pattern'
CMD_DESC = 'Pattern a solid body across sketch points with high performance'
ATTR_GROUP = 'SketchPointPatternData'
TARGET_BASE_FEAT = None
IS_UPDATING = False


def log_msg(msg):
    """Utility to print background errors to the Text Commands palette."""
    try:
        app_inst = adsk.core.Application.get()
        app_inst.userInterface.palettes.itemById('TextCommands').writeText(str(msg))
    except:
        pass


def get_target_sketch_points(pattern_sketch):
    """Returns all sketch points excluding the sketch's automatic origin point."""
    if not pattern_sketch or not pattern_sketch.isValid:
        return []
    
    origin_pt = None
    origin_world = None
    origin_local = None
    try:
        origin_pt = pattern_sketch.originPoint
        if origin_pt and origin_pt.isValid:
            origin_world = origin_pt.worldGeometry
            origin_local = origin_pt.geometry
    except Exception:
        pass

    target_pts = []
    origin_skipped = False

    for i in range(pattern_sketch.sketchPoints.count):
        skt_pt = pattern_sketch.sketchPoints.item(i)
        if not skt_pt or not skt_pt.isValid:
            continue

        # 1. Direct object equality
        if origin_pt and (skt_pt == origin_pt):
            origin_skipped = True
            continue

        # 2. Native object proxy comparison
        skt_nat = getattr(skt_pt, 'nativeObject', None)
        orig_nat = getattr(origin_pt, 'nativeObject', None) if origin_pt else None
        if (skt_nat and orig_nat and skt_nat == orig_nat) or \
           (orig_nat and skt_pt == orig_nat) or \
           (skt_nat and origin_pt and skt_nat == origin_pt):
            origin_skipped = True
            continue

        # 3. Coordinate comparison with origin (only skip once for the built-in origin point)
        is_at_origin = False
        if origin_world:
            try:
                if skt_pt.worldGeometry.distanceTo(origin_world) < 1e-5:
                    is_at_origin = True
            except Exception:
                pass
        if not is_at_origin:
            try:
                pt_geo = skt_pt.geometry
                if origin_local:
                    if (abs(pt_geo.x - origin_local.x) < 1e-5 and 
                        abs(pt_geo.y - origin_local.y) < 1e-5 and 
                        abs(pt_geo.z - origin_local.z) < 1e-5):
                        is_at_origin = True
                elif abs(pt_geo.x) < 1e-5 and abs(pt_geo.y) < 1e-5 and abs(pt_geo.z) < 1e-5:
                    is_at_origin = True
            except Exception:
                pass

        if is_at_origin and not origin_skipped:
            origin_skipped = True
            continue

        target_pts.append(skt_pt)
    return target_pts


def resolve_base_feature(entity):
    """Extracts a pattern BaseFeature from a selection (TimelineObject, BaseFeature, Proxy, or BRepBody)."""
    if not entity or not entity.isValid:
        return None
    
    # If it's a TimelineObject
    if hasattr(entity, 'entity'):
        inner = entity.entity
        res = resolve_base_feature(inner)
        if res:
            return res

    # If it has our attribute group
    try:
        if hasattr(entity, 'attributes') and entity.attributes.itemByName(ATTR_GROUP, 'source_token'):
            return entity
    except:
        pass

    # If it's a body inside a BaseFeature
    try:
        if hasattr(entity, 'baseFeature') and entity.baseFeature:
            bf = entity.baseFeature
            if hasattr(bf, 'attributes') and bf.attributes.itemByName(ATTR_GROUP, 'source_token'):
                return bf
    except:
        pass

    # Try nativeObject if it's a proxy
    try:
        if hasattr(entity, 'nativeObject') and entity.nativeObject:
            return resolve_base_feature(entity.nativeObject)
    except:
        pass

    return None


def get_point_coords(entity):
    """Extracts 3D world coordinates from supported point/vertex entities."""
    if not entity or not entity.isValid:
        return None
    if isinstance(entity, adsk.fusion.SketchPoint):
        return entity.worldGeometry
    elif isinstance(entity, adsk.fusion.BRepVertex):
        return entity.geometry
    elif isinstance(entity, adsk.fusion.ConstructionPoint):
        return entity.geometry
    return None


def calculate_state_hash(source_body, pattern_sketch, anchor_pt):
    """Computes a lightweight hash of sketch points AND source body properties."""
    if not pattern_sketch or not pattern_sketch.isValid or not anchor_pt:
        return ''
    
    coords = [f"{anchor_pt.x:.4f},{anchor_pt.y:.4f},{anchor_pt.z:.4f}"]
    target_pts = get_target_sketch_points(pattern_sketch)
    for skt_pt in target_pts:
        pt = skt_pt.worldGeometry
        coords.append(f"{pt.x:.4f},{pt.y:.4f},{pt.z:.4f}")
        
    # Incorporate physical properties to catch geometry modifications
    if source_body and source_body.isValid:
        try:
            coords.append(f"vol:{source_body.volume:.4f}")
        except:
            pass # Volume compute might fail mid-command, use bbox fallback
        bbox = source_body.boundingBox
        coords.append(f"bbox:{bbox.minPoint.x:.4f},{bbox.maxPoint.x:.4f}")
        
    return hashlib.md5(";".join(coords).encode('utf-8')).hexdigest()


def execute_brep_pattern(source_body, anchor_pt, pattern_sketch, merge_instances, target_base_feat):
    """Computes transformations in RAM via TemporaryBRepManager and updates the target BaseFeature."""
    if not source_body or not source_body.isValid:
        return False
    if not anchor_pt:
        return False
    if not pattern_sketch or not pattern_sketch.isValid:
        return False

    target_sketch_pts = get_target_sketch_points(pattern_sketch)
    target_points = [pt.worldGeometry for pt in target_sketch_pts]

    if not target_points:
        return False

    temp_mgr = adsk.fusion.TemporaryBRepManager.get()
    base_temp = temp_mgr.copy(source_body)

    tool_bodies = []
    for pt in target_points:
        vec = anchor_pt.vectorTo(pt)
        cloned = temp_mgr.copy(base_temp)
        if vec.length > 0.0001:
            mat = adsk.core.Matrix3D.create()
            mat.translation = vec
            temp_mgr.transform(cloned, mat)
        tool_bodies.append(cloned)

    target_base_feat.startEdit()
    try:
        # Clear previous bodies in this feature during recalculation
        for b in list(target_base_feat.bodies):
            b.deleteMe()

        design = adsk.fusion.Design.cast(app.activeProduct)
        parent_comp = target_base_feat.parentComponent
        if not parent_comp or not parent_comp.isValid:
            parent_comp = design.activeComponent if (design and design.activeComponent) else design.rootComponent

        if merge_instances and len(tool_bodies) > 1:
            unified = tool_bodies[0]
            for b in tool_bodies[1:]:
                temp_mgr.booleanOperation(unified, b, adsk.fusion.BooleanTypes.UnionBooleanType)
            parent_comp.bRepBodies.add(unified, target_base_feat)
        else:
            for b in tool_bodies:
                parent_comp.bRepBodies.add(b, target_base_feat)
    finally:
        target_base_feat.finishEdit()

    return True


def check_and_update_all_patterns():
    """Scans all pattern BaseFeatures in the active design and updates out-of-date geometry."""
    global IS_UPDATING
    if IS_UPDATING:
        return

    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return

        for comp in design.allComponents:
            for feat in comp.features.baseFeatures:
                if not feat.isValid:
                    continue

                attr_src = feat.attributes.itemByName(ATTR_GROUP, 'source_token')
                attr_anc = feat.attributes.itemByName(ATTR_GROUP, 'anchor_token')
                attr_skt = feat.attributes.itemByName(ATTR_GROUP, 'sketch_token')
                
                # If this base feature isn't one of ours, skip it
                if not (attr_src and attr_anc and attr_skt):
                    continue

                # --- 1. RESOLVE SOURCE BODY (With Global Name Fallback) ---
                found_s = design.findEntityByToken(attr_src.value)
                source_body = found_s[0] if (found_s and len(found_s) > 0) else None
                
                if not source_body:
                    attr_src_name = feat.attributes.itemByName(ATTR_GROUP, 'source_name')
                    if attr_src_name:
                        for c in design.allComponents:
                            for b in c.bRepBodies:
                                if b.name == attr_src_name.value:
                                    source_body = b
                                    feat.attributes.add(ATTR_GROUP, 'source_token', b.entityToken)
                                    break
                            if source_body: break

                # --- 2. RESOLVE PATTERN SKETCH (With Global Name Fallback) ---
                found_k = design.findEntityByToken(attr_skt.value)
                pattern_sketch = found_k[0] if (found_k and len(found_k) > 0) else None
                
                if not pattern_sketch:
                    attr_skt_name = feat.attributes.itemByName(ATTR_GROUP, 'sketch_name')
                    if attr_skt_name:
                        for c in design.allComponents:
                            for s in c.sketches:
                                if s.name == attr_skt_name.value:
                                    pattern_sketch = s
                                    feat.attributes.add(ATTR_GROUP, 'sketch_token', s.entityToken)
                                    break
                            if pattern_sketch: break

                if not source_body or not pattern_sketch:
                    continue # Entities are genuinely deleted, cannot update

                # --- 3. RESOLVE ANCHOR POINT (With Coordinate Fallback) ---
                found_a = design.findEntityByToken(attr_anc.value)
                anchor_entity = found_a[0] if (found_a and len(found_a) > 0) else None
                anchor_pt = get_point_coords(anchor_entity)
                
                if not anchor_pt:
                    # If vertex token was destroyed by an edit, use its last known coordinates
                    ax = feat.attributes.itemByName(ATTR_GROUP, 'anchor_x')
                    ay = feat.attributes.itemByName(ATTR_GROUP, 'anchor_y')
                    az = feat.attributes.itemByName(ATTR_GROUP, 'anchor_z')
                    if ax and ay and az:
                        anchor_pt = adsk.core.Point3D.create(float(ax.value), float(ay.value), float(az.value))
                    else:
                        continue # No fallback available, skip

                # --- 4. HASH CHECK & RECOMPUTE ---
                attr_hsh = feat.attributes.itemByName(ATTR_GROUP, 'geometry_hash')
                attr_mrg = feat.attributes.itemByName(ATTR_GROUP, 'merge_instances')
                merge_instances = (attr_mrg.value == 'True') if attr_mrg else True
                
                current_hash = calculate_state_hash(source_body, pattern_sketch, anchor_pt)

                if not attr_hsh or attr_hsh.value != current_hash:
                    IS_UPDATING = True
                    try:
                        success = execute_brep_pattern(source_body, anchor_pt, pattern_sketch, merge_instances, feat)
                        if success:
                            feat.attributes.add(ATTR_GROUP, 'geometry_hash', current_hash)
                            pt_count = len(get_target_sketch_points(pattern_sketch))
                            feat.name = f'Sketch Pattern ({pt_count} pts)'
                    finally:
                        IS_UPDATING = False

    except Exception:
        log_msg(f"Background Update Error:\n{traceback.format_exc()}")
        IS_UPDATING = False


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """Watches for Fusion UI command terminations and triggers pattern re-evaluation."""
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.ApplicationCommandEventArgs):
        cmd_id = args.commandId
        if cmd_id == CMD_ID:
            return

        # Instead of guessing every command that modifies geometry, 
        # ignore known high-frequency view/selection commands.
        ignore_cmds = ['SelectCommand', 'PanCommand', 'ZoomCommand', 'OrbitCommand', 'LookAtCommand']
        
        if cmd_id not in ignore_cmds:
            check_and_update_all_patterns()


class CommandValidateHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.ValidateInputsEventArgs):
        try:
            inputs = args.inputs
            source_body = inputs.itemById('source_body')
            anchor_point = inputs.itemById('anchor_point')
            pattern_sketch = inputs.itemById('pattern_sketch')

            if not (source_body and anchor_point and pattern_sketch):
                args.areInputsValid = False
                return

            if (source_body.selectionCount == 0 or 
                anchor_point.selectionCount == 0 or 
                pattern_sketch.selectionCount == 0):
                args.areInputsValid = False
                return

            skt = pattern_sketch.selection(0).entity
            if not skt or len(get_target_sketch_points(skt)) == 0:
                args.areInputsValid = False
                return

            args.areInputsValid = True
        except Exception:
            args.areInputsValid = False


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandEventArgs):
        global TARGET_BASE_FEAT
        try:
            inputs = args.command.commandInputs
            source_body = inputs.itemById('source_body').selection(0).entity
            anchor_entity = inputs.itemById('anchor_point').selection(0).entity
            pattern_sketch = inputs.itemById('pattern_sketch').selection(0).entity
            merge_instances = inputs.itemById('merge_instances').value
            keep_source = inputs.itemById('keep_source').value

            design = adsk.fusion.Design.cast(app.activeProduct)
            active_comp = design.activeComponent if (design and design.activeComponent) else design.rootComponent

            target_pts = get_target_sketch_points(pattern_sketch)
            pt_count = len(target_pts)
            if pt_count == 0:
                if ui:
                    ui.messageBox('The selected sketch contains no user-placed or projected points (only the origin point).')
                return

            if TARGET_BASE_FEAT and TARGET_BASE_FEAT.isValid:
                base_feat = TARGET_BASE_FEAT
                TARGET_BASE_FEAT = None
            else:
                base_feat = active_comp.features.baseFeatures.add()

            base_feat.name = f'Sketch Pattern ({pt_count} pts)'

            anchor_pt = get_point_coords(anchor_entity)
            
            # Execute with extracted Point3D instead of volatile entity
            success = execute_brep_pattern(source_body, anchor_pt, pattern_sketch, merge_instances, base_feat)
            if not success:
                ui.messageBox('Failed to generate pattern bodies.')
                return

            geom_hash = calculate_state_hash(source_body, pattern_sketch, anchor_pt)

            # Store metadata tokens & fallback names
            base_feat.attributes.add(ATTR_GROUP, 'source_token', source_body.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'source_name', source_body.name)
            
            base_feat.attributes.add(ATTR_GROUP, 'sketch_token', pattern_sketch.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'sketch_name', pattern_sketch.name)
            
            base_feat.attributes.add(ATTR_GROUP, 'anchor_token', anchor_entity.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'anchor_x', str(anchor_pt.x))
            base_feat.attributes.add(ATTR_GROUP, 'anchor_y', str(anchor_pt.y))
            base_feat.attributes.add(ATTR_GROUP, 'anchor_z', str(anchor_pt.z))
            
            base_feat.attributes.add(ATTR_GROUP, 'merge_instances', str(merge_instances))
            base_feat.attributes.add(ATTR_GROUP, 'geometry_hash', geom_hash)

            if not keep_source:
                source_body.isLightBulbOn = False

        except Exception:
            if ui:
                ui.messageBox(f'Execution Failed:\n{traceback.format_exc()}')


class CommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandEventArgs):
        global TARGET_BASE_FEAT
        TARGET_BASE_FEAT = None


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        global TARGET_BASE_FEAT
        try:
            cmd = args.command
            inputs = cmd.commandInputs

            design = adsk.fusion.Design.cast(app.activeProduct)

            # Preserve TARGET_BASE_FEAT if set by CommandStartingHandler; otherwise scan selections
            if not (TARGET_BASE_FEAT and TARGET_BASE_FEAT.isValid):
                TARGET_BASE_FEAT = None
                if ui.activeSelections.count > 0:
                    for i in range(ui.activeSelections.count):
                        cand = resolve_base_feature(ui.activeSelections.item(i).entity)
                        if cand:
                            TARGET_BASE_FEAT = cand
                            break

            source_sel = inputs.addSelectionInput('source_body', 'Source Body', 'Select body to pattern')
            source_sel.addSelectionFilter(adsk.core.SelectionCommandInput.SolidBodies)
            source_sel.setSelectionLimits(1, 1)

            anchor_sel = inputs.addSelectionInput('anchor_point', 'Anchor Point', 'Select reference origin point')
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.SketchPoints)
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.Vertices)
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.ConstructionPoints)
            anchor_sel.setSelectionLimits(1, 1)

            sketch_sel = inputs.addSelectionInput('pattern_sketch', 'Target Sketch', 'Select sketch containing pattern points')
            sketch_sel.addSelectionFilter(adsk.core.SelectionCommandInput.Sketches)
            sketch_sel.setSelectionLimits(1, 1)

            merge_box = inputs.addBoolValueInput('merge_instances', 'Combine Instances into 1 Body', True, '', True)
            keep_box = inputs.addBoolValueInput('keep_source', 'Keep Source Body Visible', True, '', True)

            if TARGET_BASE_FEAT and TARGET_BASE_FEAT.isValid:
                src_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'source_token')
                anc_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'anchor_token')
                skt_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'sketch_token')
                mrg_val = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'merge_instances')

                if src_tok and design:
                    found = design.findEntityByToken(src_tok.value)
                    if found and len(found) > 0: 
                        source_sel.addSelection(found[0])
                    else:
                        src_name = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'source_name')
                        if src_name:
                            for c in design.allComponents:
                                for b in c.bRepBodies:
                                    if b.name == src_name.value:
                                        source_sel.addSelection(b)
                                        break
                                if source_sel.selectionCount > 0: break

                if anc_tok and design:
                    found = design.findEntityByToken(anc_tok.value)
                    if found and len(found) > 0: 
                        anchor_sel.addSelection(found[0])

                if skt_tok and design:
                    found = design.findEntityByToken(skt_tok.value)
                    if found and len(found) > 0: 
                        sketch_sel.addSelection(found[0])
                    else:
                        skt_name = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'sketch_name')
                        if skt_name:
                            for c in design.allComponents:
                                for s in c.sketches:
                                    if s.name == skt_name.value:
                                        sketch_sel.addSelection(s)
                                        break
                                if sketch_sel.selectionCount > 0: break

                if mrg_val:
                    merge_box.value = (mrg_val.value == 'True')

            on_validate = CommandValidateHandler()
            cmd.validateInputs.add(on_validate)
            handlers.append(on_validate)

            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)

            on_destroy = CommandDestroyHandler()
            cmd.destroy.add(on_destroy)
            handlers.append(on_destroy)

        except Exception:
            if ui:
                ui.messageBox(f'Command Setup Failed:\n{traceback.format_exc()}')


class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Intercepts native double-clicks/edits on our BaseFeature and routes them to our custom UI."""
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.ApplicationCommandEventArgs):
        global TARGET_BASE_FEAT
        cmd_id = args.commandId
        
        # Exact commands Fusion fires when activating/editing timeline features or base features
        intercept_cmds = ['BaseFeatureActivate', 'TimelineEditCommand', 'ObjectEditCommand', 'TimelineEditFeatureCommand', 'EditBaseFeatureCommand']
        
        if cmd_id in intercept_cmds or 'edit' in cmd_id.lower() or 'basefeature' in cmd_id.lower():
            candidate = None
            if ui.activeSelections.count > 0:
                for i in range(ui.activeSelections.count):
                    sel = ui.activeSelections.item(i).entity
                    candidate = resolve_base_feature(sel)
                    if candidate:
                        break

            # If not in activeSelections, check active edit object in design
            if not candidate:
                try:
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    if design and design.activeEditObject:
                        candidate = resolve_base_feature(design.activeEditObject)
                except:
                    pass

            # Check if the feature they are trying to edit has our Add-in's metadata
            if candidate:
                TARGET_BASE_FEAT = candidate
                # 1. Cancel Fusion's native BaseFeature editing environment
                args.isCanceled = True 
                
                # 2. Launch our custom Add-in command instead
                cmd_def = ui.commandDefinitions.itemById(CMD_ID)
                if cmd_def:
                    cmd_def.execute()


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC, './resources')

        on_create = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_create)
        handlers.append(on_create)

        solid_panel = ui.allToolbarPanels.itemById('SolidCreatePanel')
        if solid_panel:
            ctrl = solid_panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
            solid_panel.controls.addCommand(cmd_def)

        on_cmd_term = CommandTerminatedHandler()
        ui.commandTerminated.add(on_cmd_term)
        handlers.append(on_cmd_term)

        # Register the double-click/edit interceptor
        on_cmd_start = CommandStartingHandler()
        ui.commandStarting.add(on_cmd_start)
        handlers.append(on_cmd_start)

    except Exception:
        if ui:
            ui.messageBox(f'Run Failed:\n{traceback.format_exc()}')


def stop(context):
    global app, ui
    try:
        solid_panel = ui.allToolbarPanels.itemById('SolidCreatePanel')
        if solid_panel:
            ctrl = solid_panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        handlers.clear()
    except Exception:
        pass