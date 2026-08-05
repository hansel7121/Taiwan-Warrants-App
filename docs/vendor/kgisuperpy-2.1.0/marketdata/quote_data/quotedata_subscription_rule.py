# Auto-generated subscription_rules from KGI.json
subscription_rules = {
    "TWStock": {
        "mode": "whitelist",
        "whitelist": [
            "*"
        ]
    },
    "USStock": {
        "mode": "whitelist",
        "whitelist": [
            "*"
        ]
    },
    "TWFuture": {
        "full_contract": {
            "mode": "blacklist",
            "blacklist": [
                "*"
            ]
        },
        "sub_contract": {
            "mode": "whitelist",
            "whitelist": [
                "TXF"
            ]
        }
    }
}