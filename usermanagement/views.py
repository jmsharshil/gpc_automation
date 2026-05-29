from django.shortcuts import render
from .models import UserActivity, WorkflowFeedback , ClientMaster
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
from django.db.models import Count, Avg
from .serializers import ClientMasterSerializer

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
            .order_by('-created_at')
        )

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
                Q(project_name__icontains=search)
            ).distinct()

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

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            queryset,
            request
        )

        result = []

        for activity in page:

            result.append({
                'activity_id': activity.id,

                'username': activity.user.username,
                'first_name': activity.user.first_name,
                'last_name': activity.user.last_name,
                'email': activity.user.email,
                'role': activity.user.role,

                'workflow': activity.get_workflow_display(),
                'workflow_key': activity.workflow,

                'client_name': activity.client_name,
                'project_name': activity.project_name,

                'created_at': activity.created_at,
                'last_login_date': activity.created_at.strftime('%d/%m/%Y'),
            })

        return paginator.get_paginated_response({
            'tracking': result
        })

    # ============================================================
    # CREATE ACTIVITY
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

        UserActivity.objects.create(
            user=user,
            workflow=workflow,
            project_name=request.data.get('project_name', ''),
            client_name=request.data.get('client_name', ''),
        )

        return Response({
            'status': 'success',
            'message': 'Activity created successfully'
        })
  
class WorkflowFeedbackView(APIView):

    def get_permissions(self):

        if self.request.method == 'GET':
            return [IsAuthenticated(), IsAdminUser()]

        return [IsAuthenticated()]

    # ============================================================
    # ADMIN FEEDBACK VIEW
    # ============================================================
    def get(self, request):

        rows = (
            WorkflowFeedback.objects
            .select_related('user')
            .order_by('-created_at')
        )

        result = []

        for row in rows:

            result.append({
                'id': row.id,

                # User Information
                'username': row.user.username,
                'first_name': row.user.first_name,
                'last_name': row.user.last_name,
                'email': row.user.email,
                'role': row.user.role,

                # Feedback Information
                'workflow': row.get_workflow_display(),
                'workflow_key': row.workflow,

                'rating': row.rating,
                'feedback': row.feedback,

                'created_at': row.created_at,
            })

        return Response({'feedbacks': result})

    # ============================================================
    # SUBMIT / UPDATE FEEDBACK
    # ============================================================
    def post(self, request):

        user = request.user

        workflow = request.data.get('workflow', '').strip()

        valid_workflows = [
            c[0] for c in WorkflowFeedback.WORKFLOW_CHOICES
        ]

        if workflow not in valid_workflows:
            return Response(
                {
                    'error': (
                        f'workflow must be one of: '
                        f'{valid_workflows}'
                    )
                },
                status=400,
            )

        # Validate rating
        try:
            rating = float(request.data.get('rating', 0))
        except (TypeError, ValueError):
            rating = 0

        valid_ratings = (
            0.5,
            1.0,
            1.5,
            2.0,
            2.5,
            3.0,
            3.5,
            4.0,
            4.5,
            5.0,
        )

        if rating not in valid_ratings:
            return Response(
                {
                    'error': (
                        f'rating must be one of '
                        f'{valid_ratings}'
                    )
                },
                status=400,
            )

        feedback_text = request.data.get(
            'feedback',
            ''
        ).strip()

        obj, created = WorkflowFeedback.objects.update_or_create(
            user=user,
            workflow=workflow,
            defaults={
                'rating': rating,
                'feedback': feedback_text,
            }
        )

        return Response({
            'status': 'updated' if not created else 'submitted',
            'workflow': workflow,
            'rating': rating,
            'feedback': feedback_text,
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

        # =====================================================
        # ACTIVITY QUERYSET
        # =====================================================

        queryset = (
            UserActivity.objects
            .select_related('user')
        )

        # =====================================================
        # FEEDBACK QUERYSET
        # =====================================================

        feedback_queryset = (
            WorkflowFeedback.objects
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

            feedback_queryset = feedback_queryset.filter(
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

            feedback_queryset = feedback_queryset.filter(
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

            feedback_queryset = feedback_queryset.filter(
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
                'user__role'
            )
            .annotate(
                count=Count('id')
            )
            .order_by('-count')
        )

        # =====================================================
        # CLIENT ANALYTICS
        # =====================================================

        clients = (
            queryset
            .exclude(client_name='')
            .values('client_name')
            .annotate(
                total_runs=Count('id'),
                unique_projects=Count(
                    'project_name',
                    distinct=True
                ),
                unique_users=Count(
                    'user',
                    distinct=True
                )
            )
            .order_by('-total_runs')
        )

        # =====================================================
        # FEEDBACK SUMMARY
        # =====================================================

        feedback_summary = {

            'total_feedbacks': (
                feedback_queryset.count()
            ),

            'average_rating': (
                feedback_queryset.aggregate(
                    avg=Avg('rating')
                )['avg'] or 0
            )
        }

        # =====================================================
        # RATING DISTRIBUTION
        # =====================================================

        rating_distribution = (
            feedback_queryset
            .values('rating')
            .annotate(
                count=Count('id')
            )
            .order_by('rating')
        )

        # =====================================================
        # WORKFLOW FEEDBACK ANALYTICS
        # =====================================================

        workflow_feedbacks = (
            feedback_queryset
            .values('workflow')
            .annotate(
                total_feedbacks=Count('id'),
                average_rating=Avg('rating')
            )
            .order_by('-average_rating')
        )

        # =====================================================
        # USER FEEDBACK ANALYTICS
        # =====================================================

        user_feedbacks = (
            feedback_queryset
            .values(
                'user__username',
                'user__email',
                'user__role'
            )
            .annotate(
                total_feedbacks=Count('id'),
                average_rating=Avg('rating')
            )
            .order_by('-average_rating')
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
                'workflow'
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
                'client_name'
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
                'project_name'
            )
            .annotate(
                total_runs=Count('id')
            )
            .order_by('date')
        )

        # =====================================================
        # RESPONSE
        # =====================================================

        return Response({

            # Activity Summary
            'summary': summary,

            # Activity Analytics
            'workflow_counts': workflow_counts,
            'client_counts': client_counts,
            'project_counts': project_counts,
            'user_counts': user_counts,

            # Client Analytics
            'clients': clients,

            # Feedback Analytics
            'feedback_summary': feedback_summary,
            'rating_distribution': rating_distribution,
            'workflow_feedbacks': workflow_feedbacks,
            'user_feedbacks': user_feedbacks,

            # Timelines
            'timeline': timeline,
            'workflow_timeline': workflow_timeline,
            'client_timeline': client_timeline,
            'project_timeline': project_timeline,
        })
        
# Client name Add API

class ClientMasterView(APIView):

    def get_permissions(self):

        # everyone can fetch dropdown
        if self.request.method == "GET":
            return [IsAuthenticated()]

        # CRUD only admin
        return [IsAuthenticated(), IsAdminUser()]

    # =========================================
    # GET ALL CLIENTS
    # =========================================

    def get(self, request):

        clients = ClientMaster.objects.filter(
            is_active=True
        )

        serializer = ClientMasterSerializer(
            clients,
            many=True
        )

        return Response({
            "clients": serializer.data
        })


    # =========================================
    # CREATE CLIENT
    # =========================================

    def post(self, request):

        serializer = ClientMasterSerializer(
            data=request.data
        )

        if serializer.is_valid():

            serializer.save()

            return Response({
                "message":"Client created",
                "data":serializer.data
            })

        return Response(
            serializer.errors,
            status=400
        )


    # =========================================
    # UPDATE CLIENT
    # =========================================

    def put(self, request):

        client_id = request.data.get("id")

        client = ClientMaster.objects.filter(
            id=client_id
        ).first()

        if not client:
            return Response(
                {"error":"Client not found"},
                status=404
            )

        serializer = ClientMasterSerializer(
            client,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response({
                "message":"Updated",
                "data":serializer.data
            })

        return Response(
            serializer.errors,
            status=400
        )


    # =========================================
    # DELETE CLIENT
    # =========================================

    def delete(self, request):

        client_id = request.GET.get("id")

        client = ClientMaster.objects.filter(
            id=client_id
        ).first()

        if not client:
            return Response(
                {"error":"Client not found"},
                status=404
            )

        client.delete()

        return Response({
            "message":"Deleted successfully"
        })