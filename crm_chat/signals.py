from django.dispatch import receiver
from django.db.models.signals import post_save
from django.conf import settings


from .models import UserOpenAISetting


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_openai_setting(sender, instance, created, **kwargs):
    if created:
        UserOpenAISetting.objects.create(user=instance)