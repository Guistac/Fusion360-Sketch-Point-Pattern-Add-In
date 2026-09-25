import os
import sys
import adsk.core
import adsk.fusion
import traceback

app = None
ui = None
handlers = []

CMD_ID = 'SketchPointPatternCmd'
CMD_NAME = 'Sketch Point Pattern'
CMD_DESC = 'Pattern a solid body across sketch points with high performance'
ATTR_GROUP = 'SketchPointPatternData'
TARGET_BASE_FEAT = None


def get_point_coords(entity):
    if isinstance(entity, adsk.fusion.SketchPoint):
        return entity.worldGeometry
    elif isinstance(entity, adsk.fusion.BRepVertex):
        return entity.geometry
    elif isinstance(entity, adsk.fusion.ConstructionPoint):
        return entity.geometry
    return None


def execute_brep_pattern(source_body, anchor_entity, pattern_sketch, merge_instances, target_base_feat):
    """Computes transformations in RAM and populates the target base feature."""
    anchor_pt = get_point_coords(anchor_entity)
    if not anchor_pt:
        return False

    target_points = []
    for i in range(pattern_sketch.sketchPoints.count):
        target_points.append(pattern_sketch.sketchPoints.item(i).worldGeometry)

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
        # Clear out any previous bodies during recompute
        for b in list(target_base_feat.bodies):
            b.deleteMe()

        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent

        if merge_instances and len(tool_bodies) > 1:
            unified = tool_bodies[0]
            for b in tool_bodies[1:]:
                temp_mgr.booleanOperation(unified, b, adsk.fusion.BooleanTypes.UnionBooleanType)
            root.bRepBodies.add(unified, target_base_feat)
        else:
            for b in tool_bodies:
                root.bRepBodies.add(b, target_base_feat)
    finally:
        target_base_feat.finishEdit()

    return True


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
            root = design.rootComponent

            pt_count = pattern_sketch.sketchPoints.count

            # Re-use existing BaseFeature if in edit mode, else create new
            if TARGET_BASE_FEAT and TARGET_BASE_FEAT.isValid:
                base_feat = TARGET_BASE_FEAT
                TARGET_BASE_FEAT = None
            else:
                base_feat = root.features.baseFeatures.add()

            base_feat.name = f'Sketch Pattern ({pt_count} pts)'

            # Fast RAM B-Rep generation
            success = execute_brep_pattern(source_body, anchor_entity, pattern_sketch, merge_instances, base_feat)
            if not success:
                ui.messageBox('Failed to generate pattern bodies.')
                return

            # Store metadata tokens on the BaseFeature for future edits
            base_feat.attributes.add(ATTR_GROUP, 'source_token', source_body.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'anchor_token', anchor_entity.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'sketch_token', pattern_sketch.entityToken)
            base_feat.attributes.add(ATTR_GROUP, 'merge_instances', str(merge_instances))

            if not keep_source:
                source_body.isLightBulbOn = False

        except Exception:
            if ui:
                ui.messageBox(f'Execution Failed:\n{traceback.format_exc()}')


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        global TARGET_BASE_FEAT
        try:
            cmd = args.command
            inputs = cmd.commandInputs

            design = adsk.fusion.Design.cast(app.activeProduct)

            # Check if user had a pattern BaseFeature selected prior to launching the command
            TARGET_BASE_FEAT = None
            if ui.activeSelections.count > 0:
                selected_ent = ui.activeSelections.item(0).entity
                candidate = None
                if isinstance(selected_ent, adsk.fusion.BaseFeature):
                    candidate = selected_ent
                elif isinstance(selected_ent, adsk.fusion.TimelineObject) and isinstance(selected_ent.entity, adsk.fusion.BaseFeature):
                    candidate = selected_ent.entity
                
                if candidate and candidate.attributes.itemByName(ATTR_GROUP, 'source_token'):
                    TARGET_BASE_FEAT = candidate

            # Inputs setup
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

            # If editing an existing feature, restore previous selections
            if TARGET_BASE_FEAT and TARGET_BASE_FEAT.isValid:
                src_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'source_token')
                anc_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'anchor_token')
                skt_tok = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'sketch_token')
                mrg_val = TARGET_BASE_FEAT.attributes.itemByName(ATTR_GROUP, 'merge_instances')

                if src_tok and design:
                    found = design.findEntityByToken(src_tok.value)
                    if found and len(found) > 0:
                        source_sel.addSelection(found[0])

                if anc_tok and design:
                    found = design.findEntityByToken(anc_tok.value)
                    if found and len(found) > 0:
                        anchor_sel.addSelection(found[0])

                if skt_tok and design:
                    found = design.findEntityByToken(skt_tok.value)
                    if found and len(found) > 0:
                        sketch_sel.addSelection(found[0])

                if mrg_val:
                    merge_box.value = (mrg_val.value == 'True')

            on_validate = CommandValidateHandler()
            cmd.validateInputs.add(on_validate)
            handlers.append(on_validate)

            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)

        except Exception:
            if ui:
                ui.messageBox(f'Command Setup Failed:\n{traceback.format_exc()}')


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_create = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_create)
        handlers.append(on_create)

        solid_panel = ui.allToolbarPanels.itemById('SolidCreatePanel')
        if solid_panel:
            ctrl = solid_panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
            solid_panel.controls.addCommand(cmd_def)

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