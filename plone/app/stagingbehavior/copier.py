from Acquisition import aq_inner, aq_parent
from ZODB.PersistentMapping import PersistentMapping
from five import grok
from z3c.relationfield import event
from zc.relation.interfaces import ICatalog
from zope import component
from zope.annotation.interfaces import IAnnotations
from zope.app.intid.interfaces import IIntIds
from zope.event import notify
from zope.schema import getFieldsInOrder

from Products.CMFCore.utils import getToolByName
from Products.DCWorkflow.DCWorkflow import DCWorkflowDefinition
from plone.app.iterate import copier
from plone.app.iterate import interfaces
from plone.app.iterate.event import AfterCheckinEvent
from plone.dexterity.utils import iterSchemata

from plone.app.stagingbehavior import STAGING_RELATION_NAME
from plone.app.stagingbehavior.interfaces import IWCAnnotator
from plone.app.stagingbehavior.interfaces import IStagingSupport
from plone.app.stagingbehavior.relation import StagingRelationValue


class ContentCopier( copier.ContentCopier, grok.Adapter ):
    grok.implements( interfaces.IObjectCopier )
    grok.context( IStagingSupport )

    def copyTo( self, container ):
        context = aq_inner( self.context )
        wc = self._copyBaseline( container )
        # get id of objects
        intids = component.getUtility( IIntIds )
        wc_id = intids.getId( wc )
        # create a relation
        relation = StagingRelationValue( wc_id )
        event._setRelation( context, STAGING_RELATION_NAME, relation )
        #
        self._handleReferences( self.context, wc, 'checkout', relation )
        return wc, relation

    def merge( self ):
        baseline = self._getBaseline()

        # delete the working copy reference to the baseline
        wc_ref = self._deleteWorkingCopyRelation()

        # reassemble references on the new baseline
        self._handleReferences( baseline, self.context, "checkin", wc_ref )

        # move the working copy to the baseline container, deleting the baseline
        new_baseline, contained = self._replaceBaseline( baseline )

        # patch the working copy with baseline info not preserved during checkout
        self._reassembleWorkingCopy( new_baseline, baseline )


        # reset wf state security directly
        workflow_tool = getToolByName(self.context, 'portal_workflow')
        for contained_new_baseline, contained_baseline in contained:
            if contained_baseline:
                try:
                    contained_new_baseline.workflow_history = \
                        PersistentMapping(
                            contained_baseline.workflow_history.items()
                        )
                except AttributeError:
                    # No workflow apparently.  Oh well.
                    pass
            wfs = workflow_tool.getWorkflowsFor(contained_new_baseline)
            for wf in wfs:
                if not isinstance( wf, DCWorkflowDefinition ):
                    continue
                wf.updateRoleMappingsFor(contained_new_baseline)

        return new_baseline

    def _do_replaceBaseline( self, baseline, wc ):
        # copy all field values from the working copy to the baseline
        for schema in iterSchemata( baseline ):
            for name, field in getFieldsInOrder( schema ):
                # Skip read-only fields
                if field.readonly:
                    continue
                try:
                    value = field.get( schema( wc ) )
                except:
                    value = None

                # TODO: We need a way to identify the DCFieldProperty
                # fields and use the appropriate set_name/get_name
                if name == 'effective':
                    baseline.effective_date = wc.effective()
                elif name == 'expires':
                    baseline.expiration_date = wc.expires()
                elif name == 'subjects':
                    baseline.setSubject(wc.Subject())
                else:
                    field.set( baseline, value )

        baseline.reindexObject()

        # copy annotations
        wc_annotations = IAnnotations(wc)
        baseline_annotations = IAnnotations(baseline)

        baseline_annotations.clear()
        baseline_annotations.update(wc_annotations)

    def _replaceBaseline( self, baseline ):
        wc_id = self.context.getId()
        wc_container = aq_parent( self.context )

        self._do_replaceBaseline( baseline, self.context )

        catalog_tool = getToolByName(self.context, 'portal_catalog')
        wc_path = '/'.join(self.context.getPhysicalPath())
        baseline_path = '/'.join(baseline.getPhysicalPath())
        wc_contained = {}
        baseline_contained = {}
        for contained, path in [(wc_contained, wc_path),
                                (baseline_contained, baseline_path)]:
            for brain in catalog_tool.searchResults(path=path):
                if brain.getPath().rstrip('/') != path.rstrip('/'):
                    contained[brain.getPath()[len(path.rstrip('/'))+1:]] = brain
        wc_contained_set = set(wc_contained.keys())
        baseline_contained_set = set(baseline_contained.keys())
        new = list(wc_contained_set - baseline_contained_set)
        delete = list(baseline_contained_set - wc_contained_set)
        update = list(wc_contained_set & baseline_contained_set)
        delete.sort(key=len, reverse=True)
        new.sort(key=len)
        contained = []
        for path in delete:
            object_ = baseline_contained[path].getObject()
            container = aq_parent(aq_inner(object_))
            container._delObject(object_.getId())
        for path in new:
            object_ = wc_contained[path].getObject()
            wc_container = aq_parent(aq_inner(object_))
            rel_path = '/'.join(
                wc_container.getPhysicalPath()
            )[len(wc_path.rstrip('/'))+1:].split('/')
            current = baseline
            for item in rel_path:
                if item in current:
                    current = current[item]
                else:
                    current = None
                    break
            if current is not None:
                baseline_container = current
                clipboard = wc_container.manage_copyObjects([object_.getId()])
                result = baseline_container.manage_pasteObjects( clipboard )
                # get a reference to the working copy
                target_id = result[0]['new_id']
                n_baseline = baseline_container._getOb( target_id )
                contained.append((n_baseline, None))
        for path in update:
            object_1 = wc_contained[path].getObject()
            object_2 = baseline_contained[path].getObject()
            self._do_replaceBaseline(object_2, object_1)
            contained.append((object_2, object_2))


        # delete the working copy
        wc_container._delObject( wc_id )

        return (baseline, contained)

    def _reassembleWorkingCopy( self, new_baseline, baseline ):
        # reattach the source's workflow history, try avoid a dangling ref
        try:
            new_baseline.workflow_history = PersistentMapping( baseline.workflow_history.items() )
        except AttributeError:
            # No workflow apparently.  Oh well.
            pass

        # reset wf state security directly
        workflow_tool = getToolByName(self.context, 'portal_workflow')
        wfs = workflow_tool.getWorkflowsFor( self.context )
        for wf in wfs:
            if not isinstance( wf, DCWorkflowDefinition ):
                continue
            wf.updateRoleMappingsFor( new_baseline )
        return new_baseline

    def _handleReferences( self, baseline, wc, mode, wc_ref ):
        storage = IWCAnnotator(baseline, None)
        if storage:
            storage.set_relation(wc_ref)

    def _deleteWorkingCopyRelation( self ):
        # delete the wc reference keeping a reference to it for its annotations
        relation = self._get_relation_to_baseline()
        relation.broken(relation.to_path)
        return relation

    def _get_relation_to_baseline( self ):
        context = aq_inner( self.context )
        # get id
        intids = component.getUtility( IIntIds )
        id = intids.getId( context )
        # ask catalog
        catalog = component.getUtility( ICatalog )
        relations = list(catalog.findRelations({ 'to_id' : id }))
        relations = filter( lambda r:r.from_attribute==STAGING_RELATION_NAME,
                            relations )
        # do we have a baseline in our relations?
        if relations and not len(relations) == 1:
            raise interfaces.CheckinException( "Baseline count mismatch" )

        if not relations or not relations[0]:
            raise interfaces.CheckinException( "Baseline has disappeared" )
        return relations[0]

    def _getBaseline( self ):
        intids = component.getUtility( IIntIds )
        relation = self._get_relation_to_baseline()
        if relation:
            baseline = intids.getObject( relation.from_id )

        if not baseline:
            raise interfaces.CheckinException( "Baseline has disappeared" )
        return baseline

    def checkin( self, checkin_message ):
        # get the baseline for this working copy, raise if not found
        baseline = self._getBaseline()
        # get a hold of the relation object
        relation = self._get_relation_to_baseline()
        # publish the event for subscribers, early because contexts are about to be manipulated
        notify(event.CheckinEvent(self.context,
                                  baseline,
                                  relation,
                                  checkin_message
                                  ))
        # merge the object back to the baseline with a copier
        copier = component.queryAdapter(self.context,
                                         interfaces.IObjectCopier)
        new_baseline = copier.merge()
        # don't need to unlock the lock disappears with old baseline deletion
        notify(AfterCheckinEvent(new_baseline, checkin_message))
        return new_baseline
