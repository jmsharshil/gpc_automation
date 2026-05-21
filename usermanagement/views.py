from django.shortcuts import render
from .models import UserActivity, WorkflowFeedback as _WorkflowFeedback
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from .permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q
from django.db.models.functions import (
    TruncDay,
    TruncWeek,
    TruncMonth,
)
from django.utils.dateparse import parse_date
from django.db.models import Count

class AdminTrackingPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
    
# User tracking Client, Project and feedback rating
class AdminPanelView(APIView):

    pagination_class = AdminTrackingPagination

    def get_permissions(self):

        if self.request.method == 'GET':
            return [IsAuthenticated(), IsAdminUser()]

        return [IsAuthenticated()]

    # ============================================================
    # ADMIN TRACKING API
    # ============================================================
    def get(self, request):

        queryset = (
            UserActivity.objects
            .select_related('user')
            .prefetch_related('feedbacks')
            .order_by('-created_at')
        )

        # ========================================================
        # SEARCH
        # ========================================================

        search = request.GET.get('search')

        if search:

            queryset = queryset.filter(
                Q(user__username__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(user__email__icontains=search) |
                Q(user__role__icontains=search) |
                Q(workflow__icontains=search) |
                Q(client_name__icontains=search) |
                Q(project_name__icontains=search) |
                Q(feedbacks__feedback__icontains=search)
            ).distinct()

        # ========================================================
        # FILTERS
        # ========================================================

        workflow = request.GET.get('workflow')
        role = request.GET.get('role')
        client_name = request.GET.get('client_name')

        if workflow:
            queryset = queryset.filter(workflow=workflow)

        if role:
            queryset = queryset.filter(user__role=role)

        if client_name:
            queryset = queryset.filter(
                client_name__icontains=client_name
            )

        # ========================================================
        # PAGINATION
        # ========================================================

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            queryset,
            request
        )

        result = []

        for activity in page:

            feedback_obj = activity.feedbacks.first()

            result.append({
                'activity_id': activity.id,

                # USER INFO
                'username': activity.user.username,
                'first_name': activity.user.first_name,
                'last_name': activity.user.last_name,
                'email': activity.user.email,
                'role': activity.user.role,

                # WORKFLOW INFO
                'workflow': activity.get_workflow_display(),
                'workflow_key': activity.workflow,
                'client_name': activity.client_name,
                'project_name': activity.project_name,

                # FEEDBACK
                'rating': (
                    feedback_obj.rating if feedback_obj else None
                ),

                'feedback': (
                    feedback_obj.feedback if feedback_obj else None
                ),

                # TIMESTAMP
                'created_at': activity.created_at,
                'last_login_date': activity.created_at.strftime('%d/%m/%Y'),
            })

        return paginator.get_paginated_response({
            'tracking': result
        })

    # ============================================================
    # CREATE WORKFLOW ACTIVITY
    # ============================================================
    def post(self, request):

        user = request.user

        workflow = request.data.get('workflow', '').strip()

        valid = [c[0] for c in UserActivity.WORKFLOW_CHOICES]

        if workflow not in valid:
            return Response(
                {'error': f'workflow must be one of: {valid}'},
                status=400,
            )

        activity = UserActivity.objects.create(
            user=user,
            workflow=workflow,
            project_name=request.data.get('project_name', ''),
            client_name=request.data.get('client_name', ''),
        )

        return Response({
            'activity_id': activity.id,
        })
  
class WorkflowFeedbackView(APIView):

    def get_permissions(self):

        # GET → admin only
        if self.request.method == 'GET':
            return [IsAuthenticated(), IsAdminUser()]

        # POST → all authenticated users
        return [IsAuthenticated()]

    # ============================================================
    # OPTIONAL ADMIN FEEDBACK VIEW
    # ============================================================
    def get(self, request):

        rows = (
            _WorkflowFeedback.objects
            .select_related('user', 'activity')
            .order_by('-created_at')
        )

        result = []

        for row in rows:

            result.append({
                'id': row.id,

                'username': row.user.username,
                'workflow': row.get_workflow_display(),

                'client_name': (
                    row.activity.client_name if row.activity else None
                ),

                'project_name': (
                    row.activity.project_name if row.activity else None
                ),

                'rating': row.rating,
                'feedback': row.feedback,

                'created_at': row.created_at,
            })

        return Response({'feedbacks': result})

    # ============================================================
    # SUBMIT FEEDBACK
    # ============================================================
    def post(self, request):

        user = request.user

        activity_id = request.data.get('activity_id')

        if not activity_id:
            return Response(
                {'error': 'activity_id is required'},
                status=400,
            )

        activity = UserActivity.objects.filter(
            id=activity_id,
            user=user
        ).first()

        if not activity:
            return Response(
                {'error': 'Invalid activity_id'},
                status=400,
            )

        # Validate rating
        try:
            rating = int(request.data.get('rating', 0))
        except (TypeError, ValueError):
            rating = 0

        if rating not in (1, 2, 3, 4, 5):
            return Response(
                {'error': 'rating must be between 1 and 5'},
                status=400,
            )

        obj = _WorkflowFeedback.objects.create(
            user=user,
            activity=activity,

            # workflow auto from activity
            workflow=activity.workflow,

            rating=rating,
            feedback=request.data.get('feedback', '').strip(),
        )

        return Response({
            'status': 'submitted',
        })
        
class AdminAnalyticsView(APIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminUser,
    ]

    def get(self, request):

        # =====================================================
        # QUERY PARAMS
        # =====================================================

        period = request.GET.get('period', 'day')

        start_date = request.GET.get('start_date')
        end_date = request.GET.get('end_date')

        workflow = request.GET.get('workflow')
        client_name = request.GET.get('client_name')
        project_name = request.GET.get('project_name')
        username = request.GET.get('username')

        queryset = (
            UserActivity.objects
            .select_related('user')
        )

        # =====================================================
        # DATE FILTER
        # =====================================================

        if start_date and end_date:

            queryset = queryset.filter(
                created_at__date__range=[
                    parse_date(start_date),
                    parse_date(end_date),
                ]
            )

        # =====================================================
        # OPTIONAL FILTERS
        # =====================================================

        if workflow:
            queryset = queryset.filter(
                workflow=workflow
            )

        if client_name:
            queryset = queryset.filter(
                client_name__icontains=client_name
            )

        if project_name:
            queryset = queryset.filter(
                project_name__icontains=project_name
            )

        if username:
            queryset = queryset.filter(
                user__username__icontains=username
            )

        # =====================================================
        # TIME GROUPING
        # =====================================================

        if period == 'day':
            trunc = TruncDay('created_at')

        elif period == 'week':
            trunc = TruncWeek('created_at')

        elif period == 'month':
            trunc = TruncMonth('created_at')

        else:
            return Response(
                {'error': 'Invalid period'},
                status=400
            )

        # =====================================================
        # SUMMARY
        # =====================================================

        summary = {

            'total_runs': queryset.count(),

            'total_users': (
                queryset
                .values('user')
                .distinct()
                .count()
            ),

            'total_clients': (
                queryset
                .exclude(client_name='')
                .values('client_name')
                .distinct()
                .count()
            ),

            'total_projects': (
                queryset
                .exclude(project_name='')
                .values('project_name')
                .distinct()
                .count()
            ),
        }

        # =====================================================
        # WORKFLOW COUNTS
        # =====================================================

        workflow_counts = (
            queryset
            .values('workflow')
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # CLIENT COUNTS
        # =====================================================

        client_counts = (
            queryset
            .exclude(client_name='')
            .values('client_name')
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # PROJECT COUNTS
        # =====================================================

        project_counts = (
            queryset
            .exclude(project_name='')
            .values('project_name')
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # USER COUNTS
        # =====================================================

        user_counts = (
            queryset
            .values(
                'user__username',
                'user__email',
            )
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # CLIENT + PROJECT COUNTS
        # =====================================================

        client_project_counts = (
            queryset
            .exclude(client_name='')
            .exclude(project_name='')
            .values(
                'client_name',
                'project_name',
            )
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # CLIENT + WORKFLOW COUNTS
        # =====================================================

        client_workflow_counts = (
            queryset
            .exclude(client_name='')
            .values(
                'client_name',
                'workflow',
            )
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # PROJECT + WORKFLOW COUNTS
        # =====================================================

        project_workflow_counts = (
            queryset
            .exclude(project_name='')
            .values(
                'project_name',
                'workflow',
            )
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # OVERALL TIMELINE
        # =====================================================

        timeline = (
            queryset
            .annotate(date=trunc)
            .values('date')
            .annotate(
                total_runs=Count('id')
            )
            .order_by('date')
        )

        # =====================================================
        # WORKFLOW TIMELINE
        # =====================================================

        workflow_timeline = (
            queryset
            .annotate(date=trunc)
            .values(
                'date',
                'workflow',
            )
            .annotate(
                total_runs=Count('id')
            )
            .order_by('date')
        )

        # =====================================================
        # CLIENT TIMELINE
        # =====================================================

        client_timeline = (
            queryset
            .exclude(client_name='')
            .annotate(date=trunc)
            .values(
                'date',
                'client_name',
            )
            .annotate(
                total_runs=Count('id')
            )
            .order_by('date')
        )

        # =====================================================
        # PROJECT TIMELINE
        # =====================================================

        project_timeline = (
            queryset
            .exclude(project_name='')
            .annotate(date=trunc)
            .values(
                'date',
                'project_name',
            )
            .annotate(
                total_runs=Count('id')
            )
            .order_by('date')
        )

        # =====================================================
        # TOP CLIENTS
        # =====================================================

        top_clients = client_counts[:10]

        # =====================================================
        # TOP PROJECTS
        # =====================================================

        top_projects = project_counts[:10]

        # =====================================================
        # TOP USERS
        # =====================================================

        top_users = user_counts[:10]

        # =====================================================
        # RESPONSE
        # =====================================================

        return Response({

            # =================================================
            # SUMMARY
            # =================================================

            'summary': summary,

            # =================================================
            # COUNTS
            # =================================================

            'workflow_counts': workflow_counts,

            'client_counts': client_counts,

            'project_counts': project_counts,

            'user_counts': user_counts,

            # =================================================
            # RELATIONAL ANALYTICS
            # =================================================

            'client_project_counts': (
                client_project_counts
            ),

            'client_workflow_counts': (
                client_workflow_counts
            ),

            'project_workflow_counts': (
                project_workflow_counts
            ),

            # =================================================
            # TIMELINES
            # =================================================

            'timeline': timeline,

            'workflow_timeline': (
                workflow_timeline
            ),

            'client_timeline': (
                client_timeline
            ),

            'project_timeline': (
                project_timeline
            ),

            # =================================================
            # TOP DATA
            # =================================================

            'top_clients': top_clients,

            'top_projects': top_projects,

            'top_users': top_users,
        })