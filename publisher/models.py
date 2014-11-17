from copy import deepcopy

from django.utils import timezone
from django.db import models

from .managers import PublisherManager, PublisherQuerySet
from .utils import assert_draft
from .signals import (
    publisher_publish_pre_save_draft, publisher_post_publish,
    publisher_pre_unpublish, publisher_post_unpublish)


class PublisherModelBase(models.Model):
    STATE_PUBLISHED = False
    STATE_DRAFT = True

    publisher_linked = models.OneToOneField(
        'self',
        related_name='publisher_draft',
        null=True,
        editable=False,
        on_delete=models.SET_NULL)
    publisher_is_draft = models.BooleanField(
        default=STATE_DRAFT,
        editable=False,
        db_index=True)
    publisher_modified_at = models.DateTimeField(
        default=timezone.now,
        editable=False)

    publisher_published_at = models.DateTimeField(null=True, editable=False)

    publisher_fields = (
        'publisher_linked',
        'publisher_is_draft',
        'publisher_modified_at',
        'publisher_draft',
    )
    publisher_ignore_fields = publisher_fields + (
        'pk',
        'id',
        'publisher_linked',
    )
    publisher_publish_empty_fields = (
        'pk',
        'id',
    )

    class Meta:
        abstract = True

    @property
    def is_draft(self):
        return self.publisher_is_draft == self.STATE_DRAFT

    @property
    def is_published(self):
        return self.publisher_is_draft == self.STATE_PUBLISHED

    @property
    def is_dirty(self):
        if not self.is_draft:
            return False

        # If the record has not been published assume dirty
        if not self.publisher_linked:
            return True

        if self.publisher_modified_at > self.publisher_linked.publisher_modified_at:
            return True

        # Check all related models to see if they have been modified
        if self.check_reverse_foreign_keys_dirty(self):
            return True

        if self.check_reverse_m2m_dirty(self):
            return True

        return False

    def check_reverse_foreign_keys_dirty(self, obj):
        reverse_foreignkeys = obj._meta.get_all_related_objects()
        for relation in reverse_foreignkeys:
            if relation.field.rel.multiple:
                relation_items = getattr(self, relation.get_accessor_name(), None)
                for item in relation_items.all():
                    tdelta = item.modified - item.created
                    if tdelta.total_seconds() > 5:
                        return True
                    self.check_reverse_foreign_keys_dirty(item)
        return False

    def check_reverse_m2m_dirty(self, obj):
        reverse_m2ms = self._meta.get_all_related_many_to_many_objects()
        for relation in reverse_m2ms:
            relation_items = getattr(self, relation.get_accessor_name(), None)
            for item in relation_items.all():
                tdelta = item.modified - item.created
                if tdelta.total_seconds() > 5:
                    return True
                self.check_reverse_m2m_dirty(item)
        return False

    @assert_draft
    def publish(self):
        if not self.is_draft:
            return

        if not self.is_dirty:
            return

        # Reference self for readability
        draft_obj = self

        # Set the published date if this is the first time the page has been published
        if not draft_obj.publisher_linked:
            draft_obj.publisher_published_at = timezone.now()

        # Duplicate the draft object and set to published
        publish_obj = self.__class__.objects.get(pk=self.pk)
        for fld in self.publisher_publish_empty_fields:
            setattr(publish_obj, fld, None)
        publish_obj.publisher_is_draft = self.STATE_PUBLISHED
        publish_obj.publisher_published_at = draft_obj.publisher_published_at

        # Link the published obj to the draft version
        publish_obj.save()

        # Check for translations, if so duplicate the object
        self.clone_translations(draft_obj, publish_obj)

        # Clone relationships
        self.clone_relations(draft_obj, publish_obj)

        # Link the draft obj to the current published version
        draft_obj.publisher_linked = publish_obj

        publisher_publish_pre_save_draft.send(sender=draft_obj.__class__, instance=draft_obj)

        draft_obj.save(suppress_modified=True)

        publisher_post_publish.send(sender=draft_obj.__class__, instance=draft_obj)

    @assert_draft
    def unpublish(self):
        if not self.is_draft or not self.publisher_linked:
            return

        publisher_pre_unpublish.send(sender=self.__class__, instance=self)
        self.publisher_linked.delete()
        self.publisher_linked = None
        self.publisher_published_at = None
        self.save()
        publisher_post_unpublish.send(sender=self.__class__, instance=self)

    @assert_draft
    def revert_to_public(self):
        """
        @todo Relook at this method. It would be nice if the draft pk did not have to change
        @toavoid Updates self to a alternative instance
        @toavoid self.__class__ = draft_obj.__class__
        @toavoid self.__dict__ = draft_obj.__dict__
        """
        if not self.publisher_linked:
            return

        # Get published obj and delete the draft
        draft_obj = self
        publish_obj = self.publisher_linked

        draft_obj.publisher_linked = None
        draft_obj.save()
        draft_obj.delete()

        # Mark the published object as a draft
        draft_obj = publish_obj
        publish_obj = None

        draft_obj.publisher_is_draft = draft_obj.STATE_DRAFT
        draft_obj.save()
        draft_obj.publish()

        return draft_obj

    def get_unique_together(self):
        return self._meta.unique_together

    def get_field(self, field_name):
        # return the actual field (not the db representation of the field)
        try:
            return self._meta.get_field_by_name(field_name)[0]
        except models.fields.FieldDoesNotExist:
            return None

    @staticmethod
    def clone_translations(src_obj, dst_obj):
        if hasattr(src_obj, 'translations'):
            for translation in src_obj.translations.all():
                translation.pk = None
                translation.master = dst_obj
                translation.save()

    def clone_relations(self, src_obj, dst_obj):
        """
            Copies related objects and associates them with the destination object
            Used when publishing as we need to be able to revert an entire group of
            related objects and not just the parent
        """

        reverse_foreignkeys = src_obj._meta.get_all_related_objects()
        for relation in reverse_foreignkeys:
            if relation.field.rel.multiple:
                relation_items = getattr(src_obj, relation.get_accessor_name(), None)
                for item in relation_items.all():
                    new_item = deepcopy(item)
                    new_item.id = None
                    setattr(new_item, relation.field.name, dst_obj)
                    new_item.save()
                    self.clone_relations(item, new_item)
        reverse_m2ms = src_obj._meta.get_all_related_many_to_many_objects()
        for relation in reverse_m2ms:
            relation_items = getattr(src_obj, relation.get_accessor_name(), None)
            for item in relation_items.all():
                new_item = deepcopy(item)
                new_item.id = None
                setattr(new_item, relation.field.name, dst_obj)
                new_item.save()
                self.clone_relations(item, new_item)

    def update_modified_at(self):
        self.publisher_modified_at = timezone.now()


class PublisherModel(PublisherModelBase):
    objects = models.Manager()
    publisher_manager = PublisherManager.for_queryset_class(PublisherQuerySet)()

    class Meta:
        abstract = True
        permissions = (
            ('can_publish', 'Can publish'),
        )

    def save(self, suppress_modified=False, **kwargs):
        if suppress_modified is False:
            self.update_modified_at()

        super(PublisherModel, self).save(**kwargs)
