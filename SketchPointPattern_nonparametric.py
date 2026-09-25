import adsk.core
import adsk.fusion
import traceback

app = None
ui = None
handlers = []

CMD_ID = 'SketchPointPatternCmd'
CMD_NAME = 'Sketch Point Pattern'
CMD_DESC = 'Pattern a solid body across sketch points'

def get_point_coords(entity):
    if isinstance(entity, adsk.fusion.SketchPoint):
        return entity.worldGeometry
    elif isinstance(entity, adsk.fusion.BRepVertex):
        return entity.geometry
    elif isinstance(entity, adsk.fusion.ConstructionPoint):
        return entity.geometry
    return None



class CommandValidateHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.ValidateInputsEventArgs):
        try:
            inputs = args.inputs
            source_body = inputs.itemById('source_body')
            anchor_point = inputs.itemById('anchor_point')
            pattern_sketch = inputs.itemById('pattern_sketch')
            op_input = inputs.itemById('op_mode')
            target_body = inputs.itemById('target_body')

            # Default to invalid if inputs aren't initialized yet
            if not (source_body and anchor_point and pattern_sketch and op_input):
                args.areInputsValid = False
                return

            # Primary selections required for all modes
            if (source_body.selectionCount == 0 or 
                anchor_point.selectionCount == 0 or 
                pattern_sketch.selectionCount == 0):
                args.areInputsValid = False
                return

            # Safely check operation mode
            if op_input.selectedItem:
                op_mode = op_input.selectedItem.name
                # Target body is only required for Join or Cut
                if op_mode in ('Join / Merge', 'Cut'):
                    if not target_body or target_body.selectionCount == 0:
                        args.areInputsValid = False
                        return
            else:
                # If no item is selected yet, default to invalid
                args.areInputsValid = False
                return

            args.areInputsValid = True

        except Exception:
            args.areInputsValid = False


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandEventArgs):
        try:
            inputs = args.command.commandInputs
            source_body = inputs.itemById('source_body').selection(0).entity
            anchor_entity = inputs.itemById('anchor_point').selection(0).entity
            pattern_sketch = inputs.itemById('pattern_sketch').selection(0).entity
            op_mode = inputs.itemById('op_mode').selectedItem.name
            merge_instances = inputs.itemById('merge_instances').value
            keep_tools = inputs.itemById('keep_tools').value

            target_body = None
            target_input = inputs.itemById('target_body')
            if target_input.isVisible and target_input.selectionCount > 0:
                target_body = target_input.selection(0).entity

            design = adsk.fusion.Design.cast(app.activeProduct)
            root = design.rootComponent
            timeline = design.timeline

            anchor_pt = get_point_coords(anchor_entity)
            if not anchor_pt:
                ui.messageBox('Could not resolve anchor point coordinates.')
                return

            # Collect target points strictly from the sketch
            target_points = []
            for i in range(pattern_sketch.sketchPoints.count):
                pt = pattern_sketch.sketchPoints.item(i).worldGeometry
                target_points.append(pt)

            if not target_points:
                ui.messageBox('No points found in target sketch.')
                return

            # 1. Fast in-memory B-Rep generation strictly for sketch points
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

            # 2. Package generated instances inside a Base Feature container
            base_feat = root.features.baseFeatures.add()
            base_feat.name = 'Pattern Instances'
            base_feat.startEdit()
            
            try:
                if op_mode == 'New Bodies':
                    if merge_instances:
                        unified = tool_bodies[0]
                        for b in tool_bodies[1:]:
                            temp_mgr.booleanOperation(unified, b, adsk.fusion.BooleanTypes.UnionBooleanType)
                        root.bRepBodies.add(unified, base_feat)
                    else:
                        for b in tool_bodies:
                            root.bRepBodies.add(b, base_feat)

                elif op_mode in ('Join / Merge', 'Cut'):
                    unified_tool = tool_bodies[0]
                    for b in tool_bodies[1:]:
                        temp_mgr.booleanOperation(unified_tool, b, adsk.fusion.BooleanTypes.UnionBooleanType)
                    
                    root.bRepBodies.add(unified_tool, base_feat)

            finally:
                base_feat.finishEdit()

            # 3. Combine with Target Body (Cut / Merge)
            combine_feat = None
            if op_mode in ('Join / Merge', 'Cut') and target_body:
                if base_feat.bodies.count == 0:
                    ui.messageBox('Failed to retrieve tool body from Base Feature.')
                    return

                active_tool_body = base_feat.bodies.item(0)

                tools_collection = adsk.core.ObjectCollection.create()
                tools_collection.add(active_tool_body)

                combine_feats = root.features.combineFeatures
                combine_input = combine_feats.createInput(target_body, tools_collection)

                if op_mode == 'Join / Merge':
                    combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
                else:
                    combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation

                combine_input.isKeepToolBodies = False
                combine_feat = combine_feats.add(combine_input)
                combine_feat.name = f'Pattern {op_mode}'

            # 4. Group created features in timeline
            try:
                base_obj = base_feat.timelineObject
                end_obj = combine_feat.timelineObject if combine_feat else base_obj
                
                if base_obj.index != end_obj.index:
                    group = timeline.timelineGroups.add(base_obj.index, end_obj.index)
                    group.name = f'Sketch Pattern ({len(target_points)} pts)'
            except Exception:
                pass

            # 5. Handle Source Body Cleanup if Keep Source Body is unchecked
            if not keep_tools:
                try:
                    source_body.deleteMe()
                except Exception:
                    source_body.isLightBulbOn = False

            if ui:
                ui.messageBox(f'Successfully patterned across {len(target_points)} points!')

        except Exception:
            if ui:
                ui.messageBox(f'Execution Failed:\n{traceback.format_exc()}')


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.InputChangedEventArgs):
        try:
            inputs = args.inputs
            changed_input = args.input

            if changed_input.id == 'op_mode':
                op_mode = changed_input.selectedItem.name
                target_body_input = inputs.itemById('target_body')
                merge_input = inputs.itemById('merge_instances')
                keep_tools_input = inputs.itemById('keep_tools')

                # Keep Source Body is visible for ALL modes now
                keep_tools_input.isVisible = True

                if op_mode == 'New Bodies':
                    target_body_input.isVisible = False
                    target_body_input.setSelectionLimits(0, 1)
                    merge_input.isVisible = True
                elif op_mode in ('Join / Merge', 'Cut'):
                    target_body_input.isVisible = True
                    target_body_input.setSelectionLimits(1, 1)
                    merge_input.isVisible = False
        except Exception:
            pass

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        try:
            cmd = args.command
            inputs = cmd.commandInputs

            # 1. Source Body
            source_sel = inputs.addSelectionInput('source_body', 'Source Body', 'Select body to pattern')
            source_sel.addSelectionFilter(adsk.core.SelectionCommandInput.SolidBodies)
            source_sel.setSelectionLimits(1, 1)

            # 2. Anchor Point
            anchor_sel = inputs.addSelectionInput('anchor_point', 'Anchor Point', 'Select reference origin point')
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.SketchPoints)
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.Vertices)
            anchor_sel.addSelectionFilter(adsk.core.SelectionCommandInput.ConstructionPoints)
            anchor_sel.setSelectionLimits(1, 1)

            # 3. Sketch
            sketch_sel = inputs.addSelectionInput('pattern_sketch', 'Target Sketch', 'Select sketch containing pattern points')
            sketch_sel.addSelectionFilter(adsk.core.SelectionCommandInput.Sketches)
            sketch_sel.setSelectionLimits(1, 1)

            # 4. Mode
            op_dropdown = inputs.addDropDownCommandInput('op_mode', 'Operation', adsk.core.DropDownStyles.LabeledIconDropDownStyle)
            op_dropdown.listItems.add('New Bodies', True)
            op_dropdown.listItems.add('Join / Merge', False)
            op_dropdown.listItems.add('Cut', False)

            # 5. Target Body
            target_sel = inputs.addSelectionInput('target_body', 'Target Body', 'Select body to combine with')
            target_sel.addSelectionFilter(adsk.core.SelectionCommandInput.SolidBodies)
            target_sel.setSelectionLimits(0, 1)
            target_sel.isVisible = False

            # 6. Options
            merge_box = inputs.addBoolValueInput('merge_instances', 'Combine Instances into 1 Body', True, '', True)
            
            # Renamed and visible by default across all modes
            keep_box = inputs.addBoolValueInput('keep_tools', 'Keep Source Body', True, '', True)
            keep_box.isVisible = True

            # Attach Handlers
            on_changed = CommandInputChangedHandler()
            cmd.inputChanged.add(on_changed)
            handlers.append(on_changed)

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

        # Register button definition
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_create = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_create)
        handlers.append(on_create)

        # Place directly in Solid -> Create panel
        solid_panel = ui.allToolbarPanels.itemById('SolidCreatePanel')
        if solid_panel:
            ctrl = solid_panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
            solid_panel.controls.addCommand(cmd_def)

        ui.messageBox('Sketch Point Pattern is active!\nLook in the Solid > Create panel or press S.')

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