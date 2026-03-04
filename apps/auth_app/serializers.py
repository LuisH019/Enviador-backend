"""Serializers da app de Autenticação."""

from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import (
    AccountSettings,
    GmailSender,
    GmailTemplate,
    WhatsAppSender,
    WhatsAppTemplate,
)


def _is_masked_secret(value) -> bool:
    if value is None:
        return False

    text = str(value).strip()
    if not text:
        return False

    allowed_mask_chars = {'*', '•', '●', '·'}
    if len(text) >= 4 and all(ch in allowed_mask_chars for ch in text):
        return True

    lowered = text.lower()
    return lowered in {'masked', 'hidden', 'not_changed', 'unchanged', 'keep', 'keep_current'}


class UserSerializer(serializers.ModelSerializer):
    """Serializer para dados do usuário."""
    
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name')
        read_only_fields = ('id',)


class UserRegisterSerializer(serializers.ModelSerializer):
    """Serializer para registro de novo usuário."""
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        label='Confirmar Senha'
    )
    
    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'password2', 'first_name', 'last_name')
        extra_kwargs = {
            'first_name': {'required': False},
            'last_name': {'required': False},
        }
    
    def validate_username(self, value):
        """Verificar se username já existe."""
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError(
                'Este nome de usuário já está registrado.'
            )
        return value
    
    def validate_email(self, value):
        """Verificar se email já está registrado e se é válido."""
        if not value:
            raise serializers.ValidationError(
                'O email é obrigatório.'
            )
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                'Este email já está registrado.'
            )
        return value
    
    def validate_password(self, value):
        """Validar senha contra as regras do Django."""
        try:
            validate_password(value)
        except ValidationError as e:
            raise serializers.ValidationError(e.messages)
        return value
    
    def validate(self, data):
        """Validar que as senhas coincidem."""
        if data['password'] != data['password2']:
            raise serializers.ValidationError({
                'password2': 'As senhas não coincidem.'
            })
        return data
    
    def create(self, validated_data):
        """Criar novo usuário."""
        validated_data.pop('password2')
        user = User.objects.create_user(**validated_data)
        return user


class LoginSerializer(serializers.Serializer):
    """Serializer para login do usuário."""
    username = serializers.CharField()
    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'}
    )
    
    def validate(self, data):
        """Autenticar usuário."""
        user = authenticate(
            username=data.get('username'),
            password=data.get('password')
        )
        
        if not user:
            raise serializers.ValidationError(
                'Credenciais inválidas. Verifique seu usuário e senha.'
            )
        
        data['user'] = user
        return data


class ChangePasswordSerializer(serializers.Serializer):
    """Serializer para mudança de senha."""
    old_password = serializers.CharField(write_only=True, required=True)
    new_password = serializers.CharField(write_only=True, required=True)
    new_password2 = serializers.CharField(write_only=True, required=True)
    
    def validate(self, data):
        """Validar senhas."""
        if data['new_password'] != data['new_password2']:
            raise serializers.ValidationError({
                'new_password2': 'As novas senhas não coincidem.'
            })
        if len(data['new_password']) < 8:
            raise serializers.ValidationError({
                'new_password': 'A nova senha deve ter pelo menos 8 caracteres.'
            })
        return data


class AccountSettingsSerializer(serializers.ModelSerializer):
    """Serializer para configurações da conta do usuário."""

    class Meta:
        model = AccountSettings
        fields = (
            'gmail_sender_email',
            'gmail_app_password',
            'whatsapp_phone_number',
            'whatsapp_access_token',
            'whatsapp_phone_number_id',
            'whatsapp_business_id',
            'whatsapp_templates',
        )

    def validate_whatsapp_templates(self, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError('whatsapp_templates deve ser um array de strings.')
        if any(not isinstance(item, str) for item in value):
            raise serializers.ValidationError('whatsapp_templates deve conter apenas strings.')
        return value


class GmailTemplateSerializer(serializers.ModelSerializer):
    """Serializer para templates de Gmail."""

    class Meta:
        model = GmailTemplate
        fields = ('id', 'title', 'subject', 'content')


class GmailSenderSerializer(serializers.ModelSerializer):
    """Serializer para remetentes de Gmail com templates."""

    senderEmail = serializers.EmailField(source='sender_email')
    appPassword = serializers.CharField(write_only=True, required=False, allow_blank=True)
    appPasswordMasked = serializers.SerializerMethodField(read_only=True)
    templates = GmailTemplateSerializer(many=True, read_only=True)

    class Meta:
        model = GmailSender
        fields = ('id', 'senderEmail', 'appPassword', 'appPasswordMasked', 'templates')

    def get_appPasswordMasked(self, obj):
        if not obj.app_password_encrypted:
            return ''
        return '********'

    def create(self, validated_data):
        plain_password = validated_data.pop('appPassword', '')
        sender = GmailSender(**validated_data)
        if plain_password:
            sender.set_app_password(plain_password)
        sender.save()
        return sender

    def update(self, instance, validated_data):
        plain_password = validated_data.pop('appPassword', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if plain_password is not None:
            plain_password = plain_password.strip() if isinstance(plain_password, str) else plain_password
            if not _is_masked_secret(plain_password):
                instance.set_app_password(plain_password)
        instance.save()
        return instance


class WhatsAppTemplateSerializer(serializers.ModelSerializer):
    """Serializer para templates de WhatsApp."""

    class Meta:
        model = WhatsAppTemplate
        fields = ('id', 'title', 'content')


class WhatsAppSenderSerializer(serializers.ModelSerializer):
    """Serializer para remetentes de WhatsApp com templates."""

    phoneNumber = serializers.CharField(source='phone_number')
    accessToken = serializers.CharField(write_only=True, required=False, allow_blank=True)
    accessTokenMasked = serializers.SerializerMethodField(read_only=True)
    phoneNumberId = serializers.CharField(source='phone_number_id')
    businessId = serializers.CharField(source='business_id')
    templates = WhatsAppTemplateSerializer(many=True, read_only=True)

    class Meta:
        model = WhatsAppSender
        fields = (
            'id',
            'phoneNumber',
            'accessToken',
            'accessTokenMasked',
            'phoneNumberId',
            'businessId',
            'templates',
        )

    def get_accessTokenMasked(self, obj):
        if not obj.access_token_encrypted:
            return ''
        return '********'

    def create(self, validated_data):
        plain_token = validated_data.pop('accessToken', '')
        sender = WhatsAppSender(**validated_data)
        if plain_token:
            sender.set_access_token(plain_token)
        sender.save()
        return sender

    def update(self, instance, validated_data):
        plain_token = validated_data.pop('accessToken', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if plain_token is not None:
            plain_token = plain_token.strip() if isinstance(plain_token, str) else plain_token
            if not _is_masked_secret(plain_token):
                instance.set_access_token(plain_token)
        instance.save()
        return instance


class AccountSettingsGmailCompatSerializer(serializers.Serializer):
    """Bloco compatível de Gmail para resposta final de settings."""

    senderEmail = serializers.CharField(allow_blank=True)
    appPassword = serializers.CharField(allow_blank=True)


class AccountSettingsWhatsAppCompatSerializer(serializers.Serializer):
    """Bloco compatível de WhatsApp para resposta final de settings."""

    phoneNumber = serializers.CharField(allow_blank=True)
    accessToken = serializers.CharField(allow_blank=True)
    phoneNumberId = serializers.CharField(allow_blank=True)
    businessId = serializers.CharField(allow_blank=True)
    templates = serializers.ListField(child=serializers.CharField(), default=list)


class AccountSettingsResponseSerializer(serializers.Serializer):
    """Contrato final de settings consumido pelo frontend."""

    gmail = AccountSettingsGmailCompatSerializer()
    whatsapp = AccountSettingsWhatsAppCompatSerializer()
    gmailSenders = GmailSenderSerializer(many=True)
    whatsappSenders = WhatsAppSenderSerializer(many=True)
