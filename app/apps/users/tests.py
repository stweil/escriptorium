from django.core import mail
from django.contrib.auth import get_user_model, get_user
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from users.models import Invitation
from users.models import User as CustomUser


User = get_user_model()


class AuthTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="test",
                                             password="test",
                                             email="test@test.com")
    
    def test_user_model(self):
        self.assertEqual(self.user.__class__, CustomUser)
    
    def test_login(self):
        with self.assertNumQueries(0):
            response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        
        with self.assertNumQueries(9):
            response = self.client.post(reverse('login'),
                                        {'username':"test",
                                         'password': "test"})
        # TODO eventually: test errors
        self.assertNotContains(response, "error", status_code=302)
        user = get_user(self.client)
        self.assertTrue(user.is_authenticated)
    
    def test_logout(self):
        self.client.login(username="test", password="test")
        with self.assertNumQueries(4):
            response = self.client.get(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        user = get_user(self.client)
        self.assertFalse(user.is_authenticated)


class InvitationTestCase(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='sender',
                                               password='test',
                                               email='sender@test.com')
        self.group = Group.objects.create(name='testgroup')
        self.sender.groups.add(self.group)

    def test_send(self):
        self.client.login(username='sender', password='test')
        with self.assertNumQueries(4):
            response = self.client.get(reverse('send-invitation'))
        self.assertEqual(response.status_code, 200)
        
        with self.assertNumQueries(7):
            response = self.client.post(reverse('send-invitation'),
                                        {"recipient_email": "recipient@test.com",
                                         "recipient_first_name": "john",
                                         "recipient_last_name": "doe",
                                         "group": self.group.pk})
        # TODO eventually: test errors
        self.assertNotContains(response, "error", status_code=302)
        
        self.assertEqual(Invitation.objects.count(), 1)
        invitation = Invitation.objects.first()
        
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue("invites" in mail.outbox[0].subject)
        self.assertTrue("recipient@test.com" in mail.outbox[0].recipients())
        url = reverse("accept-invitation", kwargs={"token": invitation.token.hex})
        self.assertTrue(url in mail.outbox[0].body)
        
        self.assertEqual(invitation.sender, self.sender)
        self.assertEqual(invitation.recipient_email, "recipient@test.com")
        self.assertEqual(invitation.group, self.group)
        self.assertEqual(invitation.workflow_state, Invitation.STATE_SENT)
        self.assertEqual(invitation.group, self.group)

    @override_settings(AUTH_PASSWORD_VALIDATORS=[])
    def test_accept(self):
        invitation = Invitation.objects.create(
            sender=self.sender,
            recipient_first_name="jim",
            recipient_last_name="doey",
            recipient_email="jim@test.com",
            group=self.group
        )

        url = reverse('accept-invitation', kwargs={'token': invitation.token})
        with self.assertNumQueries(3):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        with self.assertNumQueries(9):
            response = self.client.post(url, {
                'email': invitation.recipient_email,
                'username': 'jimd',
                'first_name': "jim",
                'last_name': "doey",
                'password1': 'test',
                'password2': 'test',
            })
        
        # TODO eventually: test errors
        self.assertNotContains(response, "error", status_code=302)

        self.assertEqual(User.objects.count(), 2)  # sender + recipient
        user = User.objects.get(username="jimd")
        self.client.login(username="jimd", password="test")
        self.assertIn(self.group, user.groups.all())

        invitation.refresh_from_db()
        self.assertEqual(invitation.workflow_state, Invitation.STATE_ACCEPTED)
        
