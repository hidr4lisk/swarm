"""WSGI de Hidr4lisk_Swarm."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swarm.settings')

application = get_wsgi_application()
