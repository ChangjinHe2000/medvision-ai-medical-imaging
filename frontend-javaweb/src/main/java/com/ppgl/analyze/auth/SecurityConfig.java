package com.ppgl.analyze.auth;

import com.nimbusds.jose.jwk.source.ImmutableSecret;
import jakarta.servlet.http.Cookie;
import java.nio.charset.StandardCharsets;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.MediaType;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;
import org.springframework.security.oauth2.server.resource.web.BearerTokenResolver;
import org.springframework.security.oauth2.server.resource.web.DefaultBearerTokenResolver;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfig {

    public static final String AUTH_COOKIE = "PPGL_ACCESS_TOKEN";

    @Bean
    SecretKey jwtSecretKey(@Value("${ppgl.auth.jwt-secret}") String secret) {
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalStateException("PPGL_AUTH_JWT_SECRET must contain at least 32 bytes");
        }
        return new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256");
    }

    @Bean
    JwtEncoder jwtEncoder(SecretKey jwtSecretKey) {
        return new NimbusJwtEncoder(new ImmutableSecret<>(jwtSecretKey));
    }

    @Bean
    JwtDecoder jwtDecoder(SecretKey jwtSecretKey) {
        return NimbusJwtDecoder.withSecretKey(jwtSecretKey).build();
    }

    @Bean
    BearerTokenResolver bearerTokenResolver() {
        DefaultBearerTokenResolver headerResolver = new DefaultBearerTokenResolver();
        return request -> {
            String headerToken = headerResolver.resolve(request);
            if (headerToken != null) {
                return headerToken;
            }
            // Cookies are shared across ports. A stale cookie from another local
            // instance must not prevent credential login or setup-state discovery.
            // Explicit bearer headers and every protected endpoint remain validated.
            String path = request.getRequestURI().substring(request.getContextPath().length());
            if (("POST".equals(request.getMethod()) && "/api/auth/login".equals(path))
                    || ("GET".equals(request.getMethod()) && "/api/auth/setup-status".equals(path))) {
                return null;
            }
            Cookie[] cookies = request.getCookies();
            if (cookies == null) {
                return null;
            }
            for (Cookie cookie : cookies) {
                if (AUTH_COOKIE.equals(cookie.getName()) && !cookie.getValue().isBlank()) {
                    return cookie.getValue();
                }
            }
            return null;
        };
    }

    @Bean
    SecurityFilterChain securityFilterChain(HttpSecurity http, BearerTokenResolver bearerTokenResolver) throws Exception {
        http
                .csrf(csrf -> csrf.disable())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(
                                "/", "/index.html", "/case-viewer.html", "/favicon.ico", "/assets/**",
                                "/api/auth/login", "/api/auth/register",
                                "/api/auth/setup-status", "/api/auth/setup", "/error"
                        ).permitAll()
                        .requestMatchers("/api/**").authenticated()
                        .anyRequest().permitAll())
                .oauth2ResourceServer(resource -> resource
                        .bearerTokenResolver(bearerTokenResolver)
                        .jwt(Customizer.withDefaults())
                        .authenticationEntryPoint((request, response, exception) -> {
                            response.setStatus(401);
                            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                            response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                            response.getWriter().write("{\"success\":false,\"message\":\"请先登录或重新登录\",\"data\":null}");
                        }))
                .exceptionHandling(errors -> errors.accessDeniedHandler((request, response, exception) -> {
                    response.setStatus(403);
                    response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                    response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                    response.getWriter().write("{\"success\":false,\"message\":\"无权访问该资源\",\"data\":null}");
                }));
        return http.build();
    }
}
