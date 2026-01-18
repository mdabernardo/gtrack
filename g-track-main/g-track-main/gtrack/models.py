# gtrack/models.py

from django.db import models
from django.contrib.auth.models import User

# This model extends the built-in User model with additional resident information.
class Resident(models.Model):
    # This creates a one-to-one link between the User and Resident models.
    # The 'id' field will be the primary key.
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    # Address of the resident.
    address = models.CharField(max_length=255, blank=True, null=True)

    # Phone number of the resident.
    phone_number = models.CharField(max_length=20, blank=True, null=True)

    # A boolean field to indicate if the resident has been verified by an admin.
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username