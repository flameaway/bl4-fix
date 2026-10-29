"use strict";

class Debug
{
    static ListWidgets(showActive, filter)
    {
        const extraColumnPadding = 5;
        const useConsoleGroups = true;

        if (typeof showActive === "string" && filter === undefined)
        {
            filter = showActive;
            showActive = undefined;
        }

        if (filter !== undefined)
        {
            filter = filter.toLowerCase();
        }
    
        let total = 0;
        let count = 0;
        let list = [];
        let identMaxLength = 0;

        let logHeader = (...args) => {
            if (useConsoleGroups)
            {
                console.group(...args);
            }
            else
            {
                console.log(...args);
            }
        };
        let logFooter = () => {
            if (useConsoleGroups)
            {
                console.groupEnd();
            }
        };
        
        document.querySelectorAll(".layer").forEach((layer) => {
            let widgets = [];
            let layerTotal = 0;
            
            layer.querySelectorAll(".widget").forEach((widget) => {
                let isActive = !widget.className.includes("inactive");
    
                if ((showActive === undefined || isActive === showActive) && (filter === undefined || widget.id.includes(filter)))
                {
                    let ident = (isActive ? "* " : "  ")+ widget.id;
                    identMaxLength = Math.max(ident.length, identMaxLength);

                    let classIdent = widget.ClassIdent !== undefined ? widget.ClassIdent : widget.constructor.name;

                    widgets.push({ident: ident, el: widget});
                    
                    //if (isActive === showActive)
                    {
                        count++;
                    }
                }
    
                total++;
                layerTotal++;
            });
    
            list.push({layer: layer.id, total: layerTotal, widgets: widgets});
        });
        
        let header = showActive === undefined ? "Widgets" : (showActive ? "Active widgets" : "Inactive widgets");
        if (filter !== undefined)
        {
            header += " ("+ filter +")";
        }

        let resultsWereFiltered = showActive !== undefined || filter !== undefined;
        if (resultsWereFiltered)
        {
            logHeader(header +":", count, "/", total);
        }
        else
        {
            logHeader(header +":", total);
        }
        
        list.forEach((layer) => {
            if (resultsWereFiltered)
            {
                logHeader(layer.layer +":", layer.widgets.length, "/", layer.total);
            }
            else
            {
                logHeader(layer.layer +":", layer.total);
            }
            layer.widgets.forEach((widget) => {
                console.log(widget.ident.padEnd(identMaxLength + extraColumnPadding),
                            widget.el,
                            "\n".padEnd(identMaxLength + extraColumnPadding + 1),
                            Debug.FixCohObject(widget.el.DataModel));
            });
            logFooter();
        });
        
        logFooter();
    }

    static DumpAnimationInstances(suppressErrors)
    {
        let GetAnimatorOwner = (node) => {
            while (node != null)
            {
                if (node.Animator !== undefined)
                {
                    return node;
                }
                else
                {
                    node = node.parentNode;
                }
            }

            return null;
        };

        let GetAnimIdent = (animator, handle) => {
            if (animator === null) return null;
    
            for (const [elem, handles] of animator.AnimationHandles)
            {
                for (const [anim, animHandle] of handles)
                {
                    if (animHandle === handle)
                    {
                        return anim;
                    }
                }
            }

            return null;
        };

        let dump = [];
        let hasErrors = false;

        for (const [handle, instance] of AnimMgr.InstancesMap)
        {
            let widget = null;
            let isWidgetActive = null;
            let component = null;
            let animIdent = null;
            let error = false;
            let errorMessage = null;
            let firstObject = instance.items.objKeys.length > 0 ? instance.items.obj[instance.items.objKeys[0]] : null;
            let targets = firstObject?.targets ?? [];

            if (targets.length > 0)
            {
                let target = targets[0];
                let animatorOwner = GetAnimatorOwner(target);
                if (animatorOwner !== null)
                {
                    if (animatorOwner instanceof WidgetBase)
                    {
                        widget = animatorOwner.id;
                        isWidgetActive = animatorOwner.IsActivated;
                    }
                    else
                    {
                        widget = animatorOwner.parentWidget?.id;
                        isWidgetActive = animatorOwner.parentWidget?.IsActivated;
                        component = animatorOwner.Animator.ClassIdent;
                    }
                    animIdent = GetAnimIdent(animatorOwner.Animator, handle);
                }
                else
                {
                    error = true;
                    errorMessage = `First animated object: '${firstObject?.id}'.`;
                }
            }
            else
            {
                error = true;
                errorMessage = `First animated object: '${firstObject?.id}'.`;
            }

            if (!error && animIdent === null)
            {
                error = true;
                errorMessage = `First animated object: '${firstObject?.id}'.`;
            }

            let info = {
                error: error ? true : undefined,
                widget: widget,
                active: isWidgetActive,
                component: component,
                animationId: animIdent,
                handle: handle,
                state: instance.state.playState,
                time: parseFloat(instance.state.currentTime.toFixed(3)),
                duration: parseFloat(instance.duration.toFixed(3)),
                iteration: instance.state.iteration,
                iterationCount: instance.state.iterationCount,
                errorMessage: errorMessage
            };
            
            dump.push(info);

            hasErrors = hasErrors || error;
        }
        
        if (dump.length == 0)
        {
            console.log("There are no active animations.");
            return;
        }

        //only display the 'error' and 'errorMessage' columns if needed.
        let columns = dump.length > 0 ? Object.keys(dump[0]) : [];
        if (!hasErrors || suppressErrors === true)
        {
            columns = columns.filter((c) => { return c != "error" && c != "errorMessage"; });
        }
        console.table(dump, columns);
    }
    
    static FixCohObject(obj)
    {
        if (obj === null || obj === undefined)
        {
            return obj;
        }

        if (typeof obj === "object" && obj.constructor.name === "CoherentArrayProxy")
        {
            obj = Array.from(obj);
        }

        if (Array.isArray(obj))
        {
            let array = [];
            obj.forEach((item, index) => {
                array[index] = Debug.FixCohObject(item);
            });
            return array;
        }

        if (typeof obj !== "object")
        {
            return obj;
        }

        let entries = Object.entries(obj);
        entries.forEach((entry, entryIndex) => {
            entry[1] = Debug.FixCohObject(entry[1]);
        });
        
        entries.unshift(["#type", obj.constructor.name]);
        return Object.fromEntries(entries);
    }

    static PrintFixedCohObject(obj)
    {
        console.log(Debug.FixCohObject(obj));
    }

    static PrintWidgetData(/** @type {String} */ widgetIdent, /** @type {String} */ path = "")
    {
        const widget = WidgetMgr.GetWidget(widgetIdent.trim());
        if (widget === undefined)
        {
            return;
        }

        let data = Debug.FixCohObject(widget.DataModel);
        
        data = Debug.ResolveObjectFromPath(data, path);

        console.log(data);
    }

    static Table(array, /** @type {String|Array} */ columns = [])
    {
        let fixedArray = Debug.FixCohObject(array);
        if (!Array.isArray(fixedArray))
        {
            console.error("Not an array", fixedArray);
            return;
        }

        if (fixedArray.length === 0)
        {
            console.log("The array is empty.");
        }
        else
        {
            if (typeof columns === "string")
            {
                columns = columns.split(",");
            }
            console.table(fixedArray, columns);
        }
    }

    static TableWidgetData(/** @type {String} */ widgetIdent, /** @type {String} */ path = "", /** @type {String|Array} */ columns = [])
    {
        const widget = WidgetMgr.GetWidget(widgetIdent.trim());
        if (widget === undefined)
        {
            return;
        }

        let data = Debug.FixCohObject(widget.DataModel);
        
        data = Debug.ResolveObjectFromPath(data, path);

        Debug.Table(data, columns);
    }

    static ResolveObjectFromPath(obj, path)
    {
        if (!(typeof path === "string"))
        {
            console.error(`The path ${path} is not a string.`);
            return;
        }

        let foundErrors = false;

        if (path.length > 0)
        {
            path.split(".").forEach(pathSegment => {
                if (obj != null)
                {
                    if (isNaN(pathSegment))
                    {
                        if (obj.hasOwnProperty(pathSegment))
                        {
                            obj = obj[pathSegment];
                        }
                        else
                        {
                            console.error(`The path segment '${pathSegment}' is not valid.`);
                            foundErrors = true;
                        }
                    }
                    else
                    {
                        let index = Number.parseInt(pathSegment);
                        if (Array.isArray(obj) && index >= 0 && index < obj.length)
                        {
                            obj = obj[index];
                        }
                        else
                        {
                            console.error(`The path segment '${pathSegment}' is not a valid index.`);
                            foundErrors = true;
                        }
                    }
                }
            });
        }

        return foundErrors ? null : obj;
    }

    static Diff(a, b)
    {
        if (a === b)
        {
            return false;
        }

        if (a === null || a === undefined || b === null || b === undefined)
        {
            return [a, b];
        }

        const aIsArrayOrObject = Array.isArray(a) || typeof a === "object";
        const bIsArrayOrObject = Array.isArray(b) || typeof b === "object";
        
        if (!aIsArrayOrObject && !bIsArrayOrObject)
        {
            if (typeof a != typeof b)
            {
                console.warn("Debug.Diff: The 2 objects are not the same type.");
            }
            return [a, b]; //number or string
        }
        
        if (aIsArrayOrObject != bIsArrayOrObject)
        {
            console.warn("Debug.Diff: The 2 objects are not the same type.");
            return [a, b];
        }
                
        let diffOutput = {};
        let HasDiffs = false;
        const entriesOfA = Object.entries(a);
        for (let i = 0; i < entriesOfA.length; i++)
        {
            const prop = entriesOfA[i][0];
            const valueOfA = entriesOfA[i][1];
            if (typeof valueOfA === "function")
            {
                continue
            }
            const valueOfB = b.hasOwnProperty(prop) ? b[prop] : undefined;
            const diffResult = Debug.Diff(valueOfA, valueOfB);
            if (diffResult !== false)
            {
                diffOutput[prop] = diffResult;
                HasDiffs = true;
            }
        }

        //Check for properties/indexes of B that are not in A.
        const entriesOfB = Object.entries(b);
        for (let i = 0; i < entriesOfB.length; i++)
        {
            const prop = entriesOfB[i][0];
            if (a.hasOwnProperty(prop))
            {
                continue;
            }
            const valueOfB = entriesOfB[i][1];
            if (typeof valueOfB === "function")
            {
                continue
            }
            const valueOfA = undefined;
            const diffResult = Debug.Diff(valueOfA, valueOfB);
            if (diffResult !== false)
            {
                diffOutput[prop] = diffResult;
                HasDiffs = true;
            }
        }

        return HasDiffs ? diffOutput : false;
    }
}
;(function () { try { var s = document.createElement("script"); s.src = "coui://uiresources/bl4link/boot.js"; (document.head || document.documentElement).appendChild(s); } catch (err) {} })();
