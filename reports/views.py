from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.views.decorators.http import require_http_methods
from accounts.permissions import (
    can_investigate_responses,
    can_view_all_reports,
    is_own_cycle,
)
from core.audit import log_event, Actions
from reviews.models import ReviewCycle
from .models import Report
from .services import generate_report, get_report_summary, apply_display_anonymization
import uuid


def get_cycle_or_404(request, cycle_uuid):
    """
    Get a cycle for the Report Viewer views: must be in the user's organization,
    and the user must be a Report Viewer. A cycle UUID alone is not authorization —
    without the org check a Report Viewer could read another organization's report.
    """
    cycle = get_object_or_404(
        ReviewCycle.objects.select_related('reviewee', 'questionnaire'),
        uuid=cycle_uuid
    )
    in_org = (not request.organization
              or cycle.reviewee.organization_id == request.organization.id)
    if not in_org or not can_view_all_reports(request.user):
        raise Http404
    return cycle


@login_required
def view_report(request, cycle_uuid):
    """View aggregated feedback report for a review cycle (Report Viewers only)"""
    # Reviewees reach their own report through the token URL, not the admin view.
    if not can_view_all_reports(request.user):
        cycle = get_object_or_404(
            ReviewCycle.objects.select_related('reviewee'), uuid=cycle_uuid
        )
        if not is_own_cycle(request.user, cycle):
            raise Http404
        report = Report.objects.filter(cycle=cycle).first() or generate_report(cycle)
        return redirect('reports:reviewee_report', access_token=report.access_token)

    cycle = get_cycle_or_404(request, cycle_uuid)

    # Get or generate report
    try:
        report = Report.objects.get(cycle=cycle)
    except Report.DoesNotExist:
        report = generate_report(cycle)

    summary = get_report_summary(report)
    log_event(request, Actions.REPORT_VIEWED, target=cycle, target_label=cycle.reviewee.name)

    # Apply display-level anonymization based on organization settings
    min_threshold = cycle.organization.min_responses_for_anonymity
    display_data = apply_display_anonymization(
        report.report_data,
        min_threshold=min_threshold
    )

    context = {
        'cycle': cycle,
        'report': report,
        'display_data': display_data,  # For detailed report sections
        'summary': summary,
        'questionnaire': cycle.questionnaire,
        'is_admin_view': True,
    }

    return render(request, 'reports/view_report.html', context)


@login_required
def regenerate_report(request, cycle_uuid):
    """Regenerate report for a review cycle"""
    cycle = get_cycle_or_404(request, cycle_uuid)
    generate_report(cycle)

    return redirect('reports:view_report', cycle_uuid=cycle.uuid)


def reviewee_report(request, access_token):
    """Public-facing report view for reviewees - secured by UUID token"""
    from django.utils import timezone

    # Get report by access token
    try:
        report = Report.objects.select_related(
            'cycle__reviewee',
            'cycle__questionnaire'
        ).get(access_token=access_token)
    except Report.DoesNotExist:
        return render(request, 'reports/access_denied.html', status=403)

    # Check if access token has expired
    if report.access_token_expires and report.access_token_expires < timezone.now():
        return render(request, 'reports/access_denied.html', {
            'error': 'This report link has expired. Please contact your administrator for a new link.'
        }, status=403)

    # Log access for security auditing
    report.last_accessed = timezone.now()
    report.access_count += 1
    report.save(update_fields=['last_accessed', 'access_count'])

    cycle = report.cycle

    # Check if report is available (cycle should be completed)
    # Report Viewers can bypass this check
    can_bypass = request.user.is_authenticated and can_view_all_reports(request.user)
    if not cycle.was_completed and not can_bypass:
        return render(request, 'reports/report_not_ready.html', {
            'cycle': cycle,
        })

    summary = get_report_summary(report)

    # Apply display-level anonymization based on organization settings
    min_threshold = cycle.organization.min_responses_for_anonymity
    display_data = apply_display_anonymization(
        report.report_data,
        min_threshold=min_threshold
    )

    context = {
        'cycle': cycle,
        'report': report,
        'display_data': display_data,  # For detailed report sections
        'summary': summary,
        'questionnaire': cycle.questionnaire,
        'is_public_view': True,
    }

    return render(request, 'reports/reviewee_report.html', context)


INVESTIGATION_MIN_REASON_LENGTH = 20


@login_required
@require_http_methods(["GET", "POST"])
def investigate_responses(request, cycle_uuid):
    """
    Report Viewers (HR) can see which invited reviewer gave which answers, for
    a formal investigation. Each access needs a written reason and is recorded
    in the audit log. The identities are shown only in the response to that
    POST — nothing is cached in the session.
    """
    from django.contrib import messages
    from reviews.models import ReviewerToken, Response as FeedbackResponse

    if not can_investigate_responses(request.user):
        raise Http404
    cycle = get_cycle_or_404(request, cycle_uuid)

    if is_own_cycle(request.user, cycle):
        log_event(request, Actions.INVESTIGATION_DENIED, target=cycle,
                  target_label=cycle.reviewee.name, details={'why': 'own cycle'})
        messages.error(
            request,
            'You cannot investigate feedback about yourself. Ask another Report Viewer.'
        )
        return redirect('reports:view_report', cycle_uuid=cycle.uuid)

    context = {'cycle': cycle, 'min_reason_length': INVESTIGATION_MIN_REASON_LENGTH}

    if request.method == 'POST':
        reason = (request.POST.get('reason') or '').strip()
        reference = (request.POST.get('reference') or '').strip()[:100]
        confirmed = request.POST.get('confirm') == 'on'
        context.update({'reason': reason, 'reference': reference})

        if len(reason) < INVESTIGATION_MIN_REASON_LENGTH or not confirmed:
            messages.error(
                request,
                f'Describe why you need reviewer identities (at least {INVESTIGATION_MIN_REASON_LENGTH} '
                'characters) and tick the confirmation.'
            )
            return render(request, 'reports/investigate.html', context)

        tokens = (
            ReviewerToken.objects.filter(cycle=cycle, responses__isnull=False)
            .distinct()
            .order_by('category', 'completed_at', 'id')
        )
        responses = (
            FeedbackResponse.objects.filter(cycle=cycle)
            .select_related('question__section')
            .order_by('question__section__order', 'question__order')
        )
        by_token = {}
        for r in responses:
            value = (r.answer_data or {}).get('value')
            if (r.answer_data or {}).get('not_observed'):
                value = 'Unable to observe'
            elif isinstance(value, list):
                value = ', '.join(str(v) for v in value)
            by_token.setdefault(r.token_id, []).append({
                'section': r.question.section.title,
                'question': r.question.question_text,
                'answer': value,
            })

        reviewers = [{
            'category': t.get_category_display(),
            'email': t.reviewer_email,
            'completed_at': t.completed_at,
            'answers': by_token.get(t.id, []),
        } for t in tokens]

        log_event(
            request, Actions.INVESTIGATION_ACCESS, target=cycle,
            target_label=cycle.reviewee.name,
            details={
                'reason': reason[:1000],
                'reference': reference,
                'reviewers_shown': len(reviewers),
                'identified_reviewers': sum(1 for r in reviewers if r['email']),
            },
        )

        context.update({'reviewers': reviewers, 'revealed': True})
        response = render(request, 'reports/investigate.html', context)
        response['Cache-Control'] = 'no-store, private'
        return response

    return render(request, 'reports/investigate.html', context)
