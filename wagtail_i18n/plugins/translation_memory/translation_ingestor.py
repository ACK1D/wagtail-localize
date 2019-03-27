from django.db import models, transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify

from wagtail.core.models import Page, PageRevision

from wagtail_i18n.models import Locale
from wagtail_i18n.segments import SegmentValue
from wagtail_i18n.segments.ingest import ingest_segments

from .models import Segment, SegmentTranslation, SegmentPageLocation, HTMLTemplatePageLocation, HTMLTemplateSegment
from .utils import get_translation_progress


@transaction.atomic
def handle_completed_revision(revision_id, src_locale, tgt_locale):
    original_page = Page.objects.get(revisions__id=revision_id).specific

    # TODO: Not currently handling edited page translations
    if original_page.has_translation(tgt_locale):
        return

    original_page_at_revision = original_page.revisions.get(id=revision_id).as_page_object()
    translated_page = original_page_at_revision.copy_for_translation(tgt_locale)

    # If the page doesn't yet have a translated parent, don't import it yet
    # This function will be called again when the translated parent is created
    if not translated_page:
        return

    # Fetch all translated segments
    segment_page_locations = (
        SegmentPageLocation.objects
        .filter(page_revision_id=revision_id)
        .annotate(
            translation=Subquery(
                SegmentTranslation.objects.filter(
                    translation_of_id=OuterRef('segment_id'),
                    locale=tgt_locale,
                ).values('text')
            )
        )
    )

    html_template_page_locations = (
        HTMLTemplatePageLocation.objects
        .filter(page_revision_id=revision_id)
        .select_related('html_template')
        .prefetch_related(
            Prefetch(
                'html_template__segments',
                queryset=(
                    HTMLTemplateSegment.objects
                    .annotate(
                        translation=Subquery(
                            SegmentTranslation.objects.filter(
                                translation_of_id=OuterRef('segment_id'),
                                locale=tgt_locale,
                            ).values('text')
                        )
                    )
                )
            )
        )
    )

    segments = []

    for page_location in segment_page_locations:
        segment = SegmentValue(page_location.path, page_location.translation)
        segments.append(segment)

    for page_location in html_template_page_locations:
        segment = SegmentValue(page_location.path, page_location.html_template.get_translated_content(tgt_locale))
        segments = []

    ingest_segments(original_page_at_revision, translated_page, src_locale, tgt_locale, segments)

    translated_page.slug = slugify(translated_page.slug)

    translated_page.save()

    # Create initial revision
    translated_page.save_revision(changed=False)

    # As we created a new page, check to see if there are any child pages that
    # have complete translations as well. This can happen if the child pages
    # were translated before the parent.

    # Check all page revisions of children of the original page that have
    # been submitted for localisation
    child_page_revision_ids = PageRevision.objects.filter(page__in=original_page.get_children()).values_list('id', flat=True)
    submitted_page_revision_ids = SegmentPageLocation.objects.values_list('page_revision_id', flat=True)
    page_revision_ids_to_check = child_page_revision_ids.intersection(submitted_page_revision_ids)

    for page_revision_id in page_revision_ids_to_check:
        total_segments, translated_segments = get_translation_progress(page_revision_id, tgt_locale)

        if total_segments == translated_segments:
            # Page revision is now complete
            handle_completed_revision(page_revision_id, src_locale, tgt_locale)


@receiver(post_save, sender=SegmentTranslation)
def on_new_segment_translation(sender, instance, created, **kwargs):
    if created:
        segment_page_locations = SegmentPageLocation.objects.filter(segment_id=instance.translation_of_id)
        html_template_page_locations = HTMLTemplatePageLocation.objects.filter(html_template__segments__segment_id=instance.translation_of_id)

        revision_ids = segment_page_locations.values_list('page_revision_id', flat=True).union(html_template_page_locations.values_list('page_revision_id', flat=True))

        # Check if any translated pages can now be created
        for page_revision_id in revision_ids:
            total_segments, translated_segments = get_translation_progress(page_revision_id, instance.locale)

            if total_segments == translated_segments:
                # FIXME
                src_locale = Locale.default()

                # Page revision is now complete
                handle_completed_revision(page_revision_id, src_locale, instance.locale)


@transaction.atomic
def ingest_translations(src_locale, tgt_locale, translations):
    for source, translation in translations:
        segment = Segment.from_text(src_locale, source)
        SegmentTranslation.from_text(segment, tgt_locale, translation)
